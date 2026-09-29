"""
Thin wrapper around paho-mqtt's client, confirmed via smoke test to need
the newer `CallbackAPIVersion.VERSION2` (paho-mqtt 2.x deprecated the old
default callback signatures). Phase 3 kept this deliberately minimal:
connect, publish, disconnect -- no retry, no reconnect-on-failure
handling, no local buffering. Phase 4 adds disconnect detection and a
non-blocking, backoff-gated reconnect attempt; buffering and replay are
`edge_gateway/buffer.py` and `edge_gateway/gateway.py`'s job, not this
module's -- this class still only ever answers "is the broker reachable
right now" and "did it just acknowledge that publish."

Delivery guarantee, made explicit (Phase 4): `publish()` returning True
means paho-mqtt's `MQTTMessageInfo.is_published()` was true -- which, for
QoS 1 (`DEFAULT_QOS` below), only ever becomes true when the client
actually receives a PUBACK from the broker (traced in paho-mqtt 2.1.0's
own source: `Client._handle_pubackcomp()` -> `_do_on_publish()` ->
`MQTTMessageInfo._published = True`). Critically, `wait_for_publish()`
does **not** raise when it times out -- it simply returns, leaving
`is_published()` false if no PUBACK arrived in time. So `publish()`
never treats "the call didn't raise" as success; it always checks
`is_published()` afterward, and a timeout, a disconnect, or a broker
that never acknowledges all correctly produce `False`, not a false
positive. That makes this "at least once, broker-acknowledged" -- not
"exactly once" (see `gateway.py`'s replay logic for the one accepted gap
in that guarantee: a crash between broker ack and buffer-row deletion).

Phase 5 adds optional TLS (`tls_ca_certs`/`tls_certfile`/`tls_keyfile`),
needed for AWS IoT Core's mutual-TLS device authentication. This is the
ONLY change Phase 5 makes here: connect()/publish()/try_reconnect() and
the buffering/backoff logic above are completely untouched, and a caller
that passes no TLS arguments (every Phase 1-4 test, and local Mosquitto
use) gets identical behavior to before. Deciding *which* broker/cert set
to use lives in edge_gateway/cloud_publisher.py, not here -- this class
still only ever knows how to be "an MQTT client pointed at one broker,"
the same job it's always had.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import paho.mqtt.client as mqtt

DEFAULT_QOS = 1  # "at least once" -- matches this being telemetry, not a
                 # once-only command.
PUBLISH_CONFIRM_TIMEOUT_SECONDS = 2.0

# Phase 4 reconnect backoff: start at 1s, double each failed attempt, cap
# at 30s. Deliberately simple (no jitter) -- there is exactly one client
# retrying here, not a fleet that could thunder-herd a broker.
INITIAL_RECONNECT_BACKOFF_SECONDS = 1.0
MAX_RECONNECT_BACKOFF_SECONDS = 30.0


class MqttPublisher:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        client_id: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        tls_ca_certs: Optional[str] = None,
        tls_certfile: Optional[str] = None,
        tls_keyfile: Optional[str] = None,
    ) -> None:
        """tls_ca_certs/tls_certfile/tls_keyfile are Phase 5 additions for
        AWS IoT Core's mutual-TLS auth (a CA bundle, a device certificate,
        and that device's private key -- all three, as *file paths*, never
        the credential contents themselves). Leave all three unset (the
        default) for local, unauthenticated Mosquitto -- exactly what
        every Phase 1-4 caller already does. All three must be given
        together or not at all: a partial set is almost certainly a
        configuration mistake, so this fails fast with ValueError instead
        of silently connecting without TLS or crashing later inside
        paho-mqtt with a less obvious error."""
        self._host = host
        self._port = port
        self._logger = logger
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self._client.on_disconnect = self._handle_disconnect
        self._connected = False

        tls_args = (tls_ca_certs, tls_certfile, tls_keyfile)
        if any(tls_args) and not all(tls_args):
            raise ValueError(
                "tls_ca_certs, tls_certfile, and tls_keyfile must all be "
                "provided together, or none of them at all."
            )
        self._tls_enabled = all(tls_args)
        if self._tls_enabled:
            self._client.tls_set(ca_certs=tls_ca_certs, certfile=tls_certfile, keyfile=tls_keyfile)

        # Phase 4 reconnect state -- see try_reconnect().
        self._reconnect_backoff_seconds = INITIAL_RECONNECT_BACKOFF_SECONDS
        self._next_reconnect_attempt_at = 0.0  # a time.monotonic() timestamp; 0 means "attempt immediately"

        # Phase 4 deterministic fault injection hook -- see
        # edge_gateway/fault_injection.py. 0 (the default) means normal
        # behavior; never touched outside tests/demos.
        self._forced_failures_remaining = 0

        # Phase 9 fault injection hook: a simulated *connection* outage
        # (see inject_connection_outage()). Both False by default, which
        # leaves every method below behaving exactly as before.
        self._simulated_outage = False
        self._simulated_link_intact = False  # real socket untouched by the simulated drop

    def connect(self, keepalive: int = 30) -> None:
        self._client.connect(self._host, self._port, keepalive=keepalive)
        self._client.loop_start()  # background thread drives the network loop
        self._connected = True
        self._reconnect_backoff_seconds = INITIAL_RECONNECT_BACKOFF_SECONDS
        self._next_reconnect_attempt_at = 0.0

    def is_tls_enabled(self) -> bool:
        """True if this instance was constructed with a full TLS cert set
        (i.e. it's talking to something like AWS IoT Core), False for a
        plain local-Mosquitto instance. Read-only -- TLS is fixed at
        construction time, not something that changes after connect()."""
        return self._tls_enabled

    def _handle_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        """paho-mqtt's on_disconnect callback (VERSION2 signature) -- the
        one place `_connected` is told about a connection loss that
        happens *after* connect() succeeded. Before Phase 4, nothing
        ever flipped `_connected` back to False on a real network drop;
        this closes exactly that gap."""
        was_connected = self._connected
        self._connected = False
        self._simulated_link_intact = False  # a real drop overrides any simulated one
        if was_connected and self._logger is not None:
            self._logger.warning(
                "MQTT disconnected", extra={"reason_code": str(reason_code)},
            )

    def is_connected(self) -> bool:
        return self._connected

    def try_reconnect(self, now: Optional[float] = None) -> bool:
        """A single, bounded, non-blocking-in-the-scheduling-sense
        reconnect attempt, gated by exponential backoff -- there is no
        background thread here, and no internal loop or sleep. The
        caller (EdgeGateway.run_once()) is expected to call this once
        per iteration of its own loop; most calls are just a cheap
        timestamp comparison that returns False immediately, and an
        actual `client.reconnect()` attempt only happens once the
        backoff window has elapsed, which is what keeps this from
        becoming a tight reconnect loop.

        Returns True if already connected, or if this call just
        reconnected successfully. Returns False if it's not yet time to
        retry, or if the attempt was made and failed.
        """
        if self._connected:
            return True

        current_time = time.monotonic() if now is None else now
        if current_time < self._next_reconnect_attempt_at:
            return False

        try:
            if self._simulated_outage:
                # Phase 9 fault injection: fail this attempt exactly like
                # an unreachable broker would, through the same backoff
                # path below.
                raise OSError("simulated connection outage (fault injection)")
            if self._simulated_link_intact:
                # The simulated outage never closed the real socket, so
                # there is nothing to re-open -- recovery is observed here,
                # by the same backoff-gated attempt a real outage uses.
                self._simulated_link_intact = False
            else:
                self._client.reconnect()
            self._connected = True
            self._reconnect_backoff_seconds = INITIAL_RECONNECT_BACKOFF_SECONDS
            self._next_reconnect_attempt_at = 0.0
            if self._logger is not None:
                self._logger.info("MQTT reconnected")
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            if self._logger is not None:
                self._logger.warning(
                    "MQTT reconnect attempt failed: %s (retrying in %.1fs)",
                    exc, self._reconnect_backoff_seconds,
                )
            self._next_reconnect_attempt_at = current_time + self._reconnect_backoff_seconds
            self._reconnect_backoff_seconds = min(
                self._reconnect_backoff_seconds * 2, MAX_RECONNECT_BACKOFF_SECONDS,
            )
            return False

    def publish(self, topic: str, payload: bytes, qos: int = DEFAULT_QOS) -> bool:
        """Returns True only once the broker has actually acknowledged
        the publish within the timeout (see this module's docstring for
        exactly what that means and how it was verified), False
        otherwise (broker unreachable, not connected, or confirmation
        timed out) -- never raises for a publish-time failure, since the
        gateway loop must keep running past one bad send."""
        if self._forced_failures_remaining > 0:
            self._forced_failures_remaining -= 1
            if self._logger is not None:
                self._logger.warning(
                    "publish forced to fail (fault injection)", extra={"topic": topic},
                )
            return False
        if not self._connected:
            if self._logger is not None:
                self._logger.error("publish attempted while not connected", extra={"topic": topic})
            return False
        try:
            info = self._client.publish(topic, payload, qos=qos)
            info.wait_for_publish(timeout=PUBLISH_CONFIRM_TIMEOUT_SECONDS)
            return info.is_published()
        except (OSError, RuntimeError, ValueError) as exc:
            if self._logger is not None:
                self._logger.error(
                    "publish failed: %s", exc, extra={"topic": topic},
                )
            return False

    def disconnect(self) -> None:
        # _simulated_link_intact: a simulated outage leaves the real
        # connection (and paho's network thread) open, so it must still be
        # closed here even though _connected is False.
        if self._connected or self._simulated_link_intact:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
            self._simulated_link_intact = False
        self._simulated_outage = False

    def inject_publish_failures(self, count: int) -> None:
        """Test/demo-only hook: makes the next `count` publish() calls
        report failure -- as if the broker were unreachable -- without
        touching the network or requiring Docker Compose to actually be
        stopped. See edge_gateway/fault_injection.py, which wraps this in
        a context manager rather than callers reaching in directly."""
        self._forced_failures_remaining = count

    def clear_injected_failures(self) -> None:
        self._forced_failures_remaining = 0

    def inject_connection_outage(self) -> None:
        """Test/demo-only hook (Phase 9): simulates the broker becoming
        unreachable, as seen from the gateway. Unlike
        inject_publish_failures() -- which leaves is_connected() True, so
        the gateway never notices an outage and never triggers its
        reconnect/replay path -- this makes the publisher report
        disconnected: publish() fails through its normal not-connected
        path, and every try_reconnect() attempt fails through its normal
        exponential-backoff path until clear_connection_outage(). The real
        socket is not touched (no network or Docker changes needed), so
        once cleared, the next backoff-gated try_reconnect() succeeds and
        EdgeGateway's existing reconnect -> replay logic takes over. See
        edge_gateway/fault_injection.py's simulated_connection_outage()."""
        self._simulated_outage = True
        if self._connected:
            self._connected = False
            self._simulated_link_intact = True
        if self._logger is not None:
            self._logger.warning("MQTT connection outage injected (fault injection)")

    def clear_connection_outage(self) -> None:
        """Ends a simulated outage. Deliberately does NOT mark the
        publisher connected: recovery is only observed by the next
        try_reconnect() attempt, exactly as with a real broker coming
        back, so the gateway's own reconnect -> replay path runs."""
        self._simulated_outage = False
        if self._logger is not None:
            self._logger.info("MQTT connection outage cleared (fault injection)")
