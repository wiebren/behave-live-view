# -*- coding: UTF-8 -*-
"""
Events of a test run that runs in other processes, like the worker processes
of a parallel test runner (behave-parallel-runner with ``--jobs N``).

The interactive view runs in the parent process. A worker process runs its
features with its own "live" formatter, which has no host there. Therefore:

* :class:`EventReceiver` (parent process): The host listens for connections
  of the worker processes (authenticated, local only) and posts the events
  that they send.
* The host tells the worker processes where it listens with the userdata
  parameter "live_view.events". A parallel test runner sends the userdata
  to its worker processes (behave-parallel-runner does).
* :class:`EventSender` (worker process): The "live" formatter sends its
  events to the host instead of writing plain status lines.

.. note:: The events of a worker process may arrive a little later than the
    result that its test runner receives. The host waits for them when the
    test run has ended (see: :meth:`EventReceiver.wait_until_done()`).
"""

import os
import threading
import time
import weakref
from multiprocessing.connection import (
    Client, Listener, answer_challenge, deliver_challenge
)


#: Name of the userdata parameter that tells where the host listens.
EVENTS_PARAM_NAME = "live_view.events"

#: Connections that may wait to be accepted. HINT: The workers of a parallel
#: test run connect at the same time; a full queue refuses a connection at
#: once on some platforms (macOS), it does not wait.
LISTEN_BACKLOG = 128

#: Delays in seconds between the attempts to connect (refused connection).
CONNECT_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.5, 1.0)


class EventReceiver:
    """Receives the events of other processes (in the parent process)."""

    def __init__(self, post_event):
        self.post_event = post_event
        self.authkey = os.urandom(32)
        # -- HINT: Authenticates each connection itself (after accept).
        self.listener = Listener(backlog=LISTEN_BACKLOG)
        #: Value of the userdata parameter, as "<authkey>:<address>".
        self.param_value = u"%s:%s" % (self.authkey.hex(),
                                       self.listener.address)
        self.event_count = 0
        self._open_connections = 0
        self._closed = False
        self._condition = threading.Condition()
        self._post_lock = threading.Lock()
        thread = threading.Thread(target=self._accept_connections,
                                  name="live-view-events", daemon=True)
        thread.start()

    def _accept_connections(self):
        while not self._closed:
            try:
                connection = self.listener.accept()
            except Exception:   # pylint: disable=broad-except
                # -- CLOSED (loop ends) or bad connection.
                time.sleep(0.05)    # -- AVOID: Busy loop if it persists.
                continue
            with self._condition:
                self._open_connections += 1
            threading.Thread(target=self._receive_events, args=(connection,),
                             name="live-view-events-reader",
                             daemon=True).start()

    def _receive_events(self, connection):
        try:
            # -- AUTHENTICATE: Like Listener.accept() with an authkey does.
            # HINT: Done after the connection is counted, the other process
            # is connected when this is done (see: wait_until_done()).
            try:
                deliver_challenge(connection, self.authkey)
                answer_challenge(connection, self.authkey)
            except Exception:   # pylint: disable=broad-except
                return      # -- NOT AUTHENTICATED: Or gone.
            while True:
                try:
                    event = connection.recv()
                except Exception:   # pylint: disable=broad-except
                    break   # -- CLOSED: Process is done (or has died).
                if not isinstance(event, dict):
                    continue
                # -- HINT: One event at a time (the host is not reentrant).
                with self._post_lock:
                    self.event_count += 1
                    try:
                        self.post_event(event)
                    except Exception:   # pylint: disable=broad-except
                        pass    # -- MALFORMED EVENT: Must not stop the others.
        finally:
            connection.close()
            with self._condition:
                self._open_connections -= 1
                self._condition.notify_all()

    def wait_until_done(self, timeout=None):
        """Wait until all connected processes have closed their connection
        (all their events are posted then).

        HINT: A connection is counted before its sender is connected (the
        authentication is done afterwards).

        :return: True, if all connections are closed (False: timeout).
        """
        with self._condition:
            return self._condition.wait_for(
                lambda: self._open_connections == 0, timeout)

    def close(self):
        self._closed = True
        try:
            self.listener.close()
        except OSError:
            pass


# -----------------------------------------------------------------------------
# SENDER SIDE: In a worker process
# -----------------------------------------------------------------------------
_open_senders = weakref.WeakSet()
_registered_at_fork = False


def _close_senders_in_forked_child():
    """A child process that a step forks must not keep a connection open:
    the host would wait for its end (see: EventReceiver.wait_until_done()).
    """
    for sender in list(_open_senders):
        sender.close()      # -- HINT: Closes only the copy of this child.


class EventSender:
    """Sends the events of a "live" formatter to the host (in a worker
    process of a parallel test run).
    """

    def __init__(self, connection):
        self.connection = connection
        _open_senders.add(self)

    @classmethod
    def connect_for(cls, config):
        """Connect to the host that the configuration names
        (see: :data:`EVENTS_PARAM_NAME`).

        :return: Sender object, or None (no host is named or reachable).
        """
        # pylint: disable=global-statement
        global _registered_at_fork
        userdata = getattr(config, "userdata", None) or {}
        value = userdata.get(EVENTS_PARAM_NAME)
        if not value:
            return None
        authkey_hex, _, address = (u"%s" % value).partition(u":")
        connection = None
        for delay in CONNECT_RETRY_DELAYS + (None,):
            try:
                connection = Client(address,
                                    authkey=bytes.fromhex(authkey_hex))
                break
            except ConnectionRefusedError:
                if delay is None:
                    return None
                time.sleep(delay)   # -- BUSY HOST: Too many connect at once.
            except Exception:   # pylint: disable=broad-except
                return None     # -- HOST IS GONE (or: wrong parameter value).
        if not _registered_at_fork and hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=_close_senders_in_forked_child)
            _registered_at_fork = True
        return cls(connection)

    def send(self, event):
        connection = self.connection
        if connection is None:
            return
        try:
            connection.send(event)
        except Exception:   # pylint: disable=broad-except
            self.close()    # -- HOST IS GONE: The events are not needed.

    def close(self):
        connection, self.connection = self.connection, None
        _open_senders.discard(self)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
