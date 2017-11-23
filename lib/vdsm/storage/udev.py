#
# Copyright 2009-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import

import inspect
import logging
import os
import pyudev
import threading

from collections import namedtuple

MultipathEvent = namedtuple("MultipathEvent",
                            "type, mpath_uuid, path, valid_paths")

MPATH_REMOVED = "removed"
PATH_FAILED = "failed"
PATH_REINSTATED = "reinstated"


def create_observer(monitor, callback, name):
    argspec = inspect.getargspec(pyudev.MonitorObserver.__init__)
    if "callback" in argspec.args:
        # pylint: disable=no-value-for-parameter
        return pyudev.MonitorObserver(monitor,
                                      callback=callback,
                                      name=name)
    else:
        def event_handler(action, device):
            callback(device)
        return pyudev.MonitorObserver(monitor, event_handler, name=name)


class MultipathListener(object):
    log = logging.getLogger("storage.udev")

    def __init__(self):
        self._lock = threading.Lock()
        self._callbacks = set()
        self._observer = None

    def start(self):
        """
        Start listening to udev events asynchronously in an observer thread.
        Received events will be forwarded to registered callbacks.
        Once the MultipathListener is started, it is safe to check the current
        system state, as events won't be lost.
        """
        self.log.info("Starting multipath event listener")
        with self._lock:
            if self._observer is not None:
                raise AssertionError("Listener already started")
            # The monitor is created here so that when the observer is stopped,
            # it will remove the last reference to the monitor,
            # closing the udev connection.
            context = pyudev.Context()
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by("block", device_type="disk")
            self._observer = create_observer(monitor,
                                             self._callback,
                                             name="mpathlistener")
            # The monitor is started in order to make sure
            # that no events are lost
            monitor.start()
            self._observer.start()

    def stop(self):
        self.log.info("Stopping multipath event listener")
        with self._lock:
            if self._observer is None:
                return
            self._observer.stop()
            self._observer = None

    def register(self, callback):
        """
        The callback will be invoked with a MultipathEvent instance
        when receiving an event.

        The callback must never block, blocking will delay receiving
        multipath events for the entire system.
        If the callback need to block, it should add the events to a queue
        and do the blocking operation in another thread.

        The caller is responsible to remove the callback when it is not needed.

        Arguments:
            callback: A callable that will be invoked with a MultipathEvent

        """
        self.log.info("Registering multipath event callback %s", callback)
        with self._lock:
            if callback in self._callbacks:
                raise AssertionError("Callback already registered")
            self._callbacks.add(callback)

    def unregister(self, callback):
        self.log.info("Unregistering multipath event callback %s", callback)
        with self._lock:
            if callback not in self._callbacks:
                raise AssertionError("Callback not registered")
            self._callbacks.remove(callback)

    def _block_device_name(self, dev):
        """
        'dev' is a string in the following format: 'major:minor', as received
        from the multipath event 'DM_PATH' property.
        This method will return the friendly name of the path, e.g. "sda"
        """
        return os.path.basename(os.readlink("/sys/dev/block/" + dev))

    def _callback(self, device):
        self.log.debug("Received udev event (action=%s, device=%s)",
                       device.action, device)
        try:
            event = self._detect_event(device)
        except Exception as e:
            self.log.exception("Error detecting udev event: %s", e)
            return

        if event:
            self._notify_observers(event)

    def _detect_event(self, device):
        mpath_uuid = device.get("DM_UUID", "")
        if not mpath_uuid.startswith("mpath-"):
            return None
        mpath_uuid = mpath_uuid[6:]

        if device.action == "change":
            dm_action = device.get("DM_ACTION")
            if dm_action == "PATH_FAILED":
                event_type = PATH_FAILED
            elif dm_action == "PATH_REINSTATED":
                event_type = PATH_REINSTATED
            else:
                self.log.debug("Unsupported DM_ACTION %r", dm_action)
                return
            valid_paths = int(device.get("DM_NR_VALID_PATHS"))
            path = self._block_device_name(device.get("DM_PATH"))
        elif device.action == "remove":
            event_type = MPATH_REMOVED
            valid_paths = None
            path = None
        else:
            return None

        event = MultipathEvent(event_type, mpath_uuid, path, valid_paths)
        self.log.debug("Sending %s", event)
        return event

    def _notify_observers(self, event):
        with self._lock:
            callbacks = list(self._callbacks)

        for cb in callbacks:
            try:
                cb(event)
            except Exception as e:
                self.log.exception("Unhandled exception in %s: %s", cb, e)
