"""
End-to-end UDS integration test: a real PowertrainUDSServer, hosted on a
real isotp.CanStack, exchanging ISO-TP-segmented frames with a real
udsoncan.client.Client (via UDSTester) over the same virtual CAN bus
python-can uses in Phase 1 -- exactly the "smoke test but as a permanent
test" this project's workflow calls for. Unlike test_uds_server.py, this
proves the actual wire-level plumbing (isotp segmentation, udsoncan
encode/decode) works together, not just the handler logic.
"""

import struct
import threading

import pytest
from udsoncan import Request
from udsoncan.exceptions import NegativeResponseException
from udsoncan.services import ReadDataByIdentifier

from common.diagnostic_schema import DEFAULT_SIMULATED_VIN, DID_ENGINE_RPM, DID_VEHICLE_SPEED_KPH
from common.telemetry_schema import DEFAULT_VEHICLE_ID
from simulation.can_bus import get_bus
from simulation.uds.uds_client import UDSTester
from simulation.uds.uds_server import PowertrainUDSServer, make_server_stack, run_server


class _FakePowertrainECU:
    def __init__(self, speed_kph: float, rpm: float) -> None:
        self.speed_kph = speed_kph
        self.rpm = rpm


@pytest.fixture
def running_server():
    """Starts a real PowertrainUDSServer on its own thread against the
    real virtual bus, and tears it down afterward."""
    server_bus = get_bus()
    server = PowertrainUDSServer(
        session_id="integration-test-session",
        vehicle_id=DEFAULT_VEHICLE_ID,
        powertrain_ecu=_FakePowertrainECU(speed_kph=88.8, rpm=4200.0),
    )
    stack = make_server_stack(server_bus)
    stop_event = threading.Event()
    events_received = []

    thread = threading.Thread(
        target=run_server, args=(server, stack, stop_event, events_received.append), daemon=True,
    )
    thread.start()

    yield events_received

    stop_event.set()
    thread.join(timeout=2)
    server_bus.shutdown()


def test_client_reads_vin_over_real_bus(running_server):
    client_bus = get_bus()
    with UDSTester(client_bus) as tester:
        vin = tester.read_did(0xF190)

    assert vin == DEFAULT_SIMULATED_VIN
    assert vin != DEFAULT_VEHICLE_ID
    client_bus.shutdown()


def test_client_reads_live_speed_and_rpm_over_real_bus(running_server):
    client_bus = get_bus()
    with UDSTester(client_bus) as tester:
        speed = tester.read_did(DID_VEHICLE_SPEED_KPH)
        rpm = tester.read_did(DID_ENGINE_RPM)

    assert speed == pytest.approx(88.8, abs=0.1)
    assert rpm == pytest.approx(4200.0, abs=1.0)
    client_bus.shutdown()


def test_client_enters_extended_session_over_real_bus(running_server):
    client_bus = get_bus()
    with UDSTester(client_bus) as tester:
        response = tester.enter_extended_session()

    assert response.positive is True
    client_bus.shutdown()


def test_client_reads_dtcs_over_real_bus(running_server):
    client_bus = get_bus()
    with UDSTester(client_bus) as tester:
        dtcs = tester.read_dtcs()

    assert len(dtcs) == 2
    client_bus.shutdown()


def test_unsupported_did_raises_negative_response_on_client(running_server):
    # udsoncan.Client.read_data_by_identifier() validates the DID against
    # its OWN local codec config before ever sending a request -- an
    # unknown DID never reaches the wire, it raises ConfigError locally.
    # To exercise the server's *actual* over-the-wire negative response
    # (RequestOutOfRange), we bypass that convenience wrapper and send a
    # raw Request directly, the way Client.read_data_by_identifier does
    # internally once the DID passes its local check.
    client_bus = get_bus()
    with UDSTester(client_bus) as tester:
        raw_request = Request(service=ReadDataByIdentifier, data=struct.pack(">H", 0x1234))
        with pytest.raises(NegativeResponseException):
            tester._client.send_request(raw_request)

    client_bus.shutdown()


def test_server_emits_one_diagnostic_event_per_transaction(running_server):
    events_received = running_server
    client_bus = get_bus()
    with UDSTester(client_bus) as tester:
        tester.read_did(0xF190)
    client_bus.shutdown()

    # Give the server thread a brief moment to append the event.
    import time
    time.sleep(0.2)
    assert len(events_received) == 1
    assert events_received[0].service_name == "ReadDataByIdentifier"
    assert events_received[0].is_positive_response is True
