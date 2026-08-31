"""
Tests for the simulated ECUs and their interaction with the virtual CAN
bus.

Value-generation tests call tick() directly -- fast and deterministic, no
real time or bus involved. One test per ECU additionally proves the
"virtual CAN bus" part of Phase 1 actually works: a frame sent from one
Bus handle is received by a second, separate Bus handle on the same
channel and decodes back correctly.
"""

import pytest

from common.can_signal_map import SIGNAL_REGISTRY, decode_signal
from simulation.can_bus import get_bus, send_event
from simulation.ecus.battery_ecu import BatteryECU
from simulation.ecus.body_ecu import BodyECU
from simulation.ecus.powertrain_ecu import PowertrainECU

SESSION_ID = "test-session"
VEHICLE_ID = "SIM-VEHICLE-01"

_ECU_CASES = [
    (PowertrainECU, {0x100, 0x101, 0x102}),
    (BatteryECU, {0x200, 0x201, 0x202, 0x203}),
    (BodyECU, {0x300, 0x301, 0x302, 0x303}),
]


@pytest.mark.parametrize("ecu_cls, expected_can_ids", _ECU_CASES)
def test_tick_produces_one_event_per_owned_signal_within_valid_range(ecu_cls, expected_can_ids):
    ecu = ecu_cls(SESSION_ID, VEHICLE_ID, rng_seed=42)
    for _ in range(50):  # many ticks, to exercise the random drift thoroughly
        events = ecu.tick()
        can_ids_seen = {event.can_id for event in events}
        assert can_ids_seen == expected_can_ids
        for event in events:
            low, high = SIGNAL_REGISTRY[event.can_id].valid_range
            assert low <= event.value <= high, (
                f"{event.signal_name.value}={event.value} outside documented range [{low}, {high}]"
            )


def test_tick_is_reproducible_with_the_same_seed():
    """A fixed rng_seed must produce identical output -- this is what keeps
    the test suite deterministic in CI instead of occasionally flaky."""
    ecu_a = PowertrainECU(SESSION_ID, VEHICLE_ID, rng_seed=7)
    ecu_b = PowertrainECU(SESSION_ID, VEHICLE_ID, rng_seed=7)
    for _ in range(10):
        values_a = [event.value for event in ecu_a.tick()]
        values_b = [event.value for event in ecu_b.tick()]
        assert values_a == values_b


@pytest.mark.parametrize("ecu_cls, expected_can_ids", _ECU_CASES)
def test_ecu_frames_are_receivable_on_the_virtual_bus(ecu_cls, expected_can_ids):
    """
    Proves the virtual CAN bus itself works end to end for this ECU: frames
    sent from one Bus handle are received by a second, independent Bus
    handle on the same channel, and decode back to the signals this ECU
    owns.
    """
    ecu = ecu_cls(SESSION_ID, VEHICLE_ID, rng_seed=1)
    sender_bus = get_bus()
    receiver_bus = get_bus()
    try:
        events = ecu.tick()
        for event in events:
            send_event(sender_bus, event)

        received_can_ids = set()
        for _ in events:
            message = receiver_bus.recv(timeout=1.0)
            assert message is not None, "expected a frame on the virtual bus but none arrived"
            decoded = decode_signal(message.arbitration_id, message.data)
            received_can_ids.add(decoded.can_id)

        assert received_can_ids == expected_can_ids
    finally:
        sender_bus.shutdown()
        receiver_bus.shutdown()
