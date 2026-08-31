"""
Shared virtual CAN bus setup for the simulation, plus the small amount of
"bus plumbing" (sending a TelemetryEvent, running an ECU's tick loop) that
is identical across all 3 ECUs and doesn't belong duplicated in each of
them.

Why the "virtual" interface: python-can's virtual backend is pure Python --
no SocketCAN kernel module, no elevated privileges -- so it behaves
identically in this sandbox, in Docker, and in GitHub Actions. Its
trade-off, confirmed with a smoke test while implementing this phase, is
that multiple Bus() instances only see each other's frames when they share
the same `channel` name *within the same process* (it's an in-memory
registry, not a real inter-process transport). That's the reason every ECU
in this simulation runs as a thread inside one Python process, rather than
as a separate OS process the way real, physically separate ECUs would be.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import can

from common.can_signal_map import encode_signal
from common.telemetry_schema import TelemetryEvent

# All ECUs and all tests use this one channel name so they land on the same
# virtual bus. Centralising it here (instead of each file hardcoding the
# string) means there's exactly one place to change it.
VIRTUAL_CHANNEL = "vehicle-sim"


def get_bus() -> can.BusABC:
    """Open a new handle onto the shared virtual CAN bus."""
    return can.Bus(channel=VIRTUAL_CHANNEL, interface="virtual")


def send_event(bus: can.BusABC, event: TelemetryEvent, logger: Optional[logging.Logger] = None) -> None:
    """Encode a TelemetryEvent per the signal map and send it as a CAN frame."""
    frame = can.Message(
        arbitration_id=event.can_id,
        data=encode_signal(event.can_id, event.value),
        is_extended_id=False,
    )
    bus.send(frame)
    if logger is not None:
        logger.debug(
            "sent can_id=0x%03X signal=%s value=%s %s",
            event.can_id, event.signal_name.value, event.value, event.unit,
        )


def run_ecu(
    ecu,
    bus: can.BusABC,
    stop_event: threading.Event,
    interval_seconds: float,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Repeatedly call ecu.tick() and send every resulting TelemetryEvent,
    waiting `interval_seconds` between ticks, until stop_event is set.

    This loop is the same for every ECU, which is why it lives here instead
    of being copy-pasted into each ECU class -- each ECU only needs to know
    *what* to generate (tick()), not *how* to run on a timer and talk to
    the bus.
    """
    while not stop_event.is_set():
        for event in ecu.tick():
            send_event(bus, event, logger=logger)
        stop_event.wait(interval_seconds)
