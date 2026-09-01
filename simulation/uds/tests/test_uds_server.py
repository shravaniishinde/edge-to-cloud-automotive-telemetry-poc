"""
Direct unit tests for PowertrainUDSServer.handle_request -- no CAN bus
involved. Request payloads are built with udsoncan's own
`<Service>.make_request(...)` helpers (the same byte-level format a real
client would send), so these tests pin down the server's request-handling
logic in isolation and run in milliseconds. The full round trip over a
real virtual CAN bus is covered separately in test_uds_integration.py.
"""

import pytest
from udsoncan.services import DiagnosticSessionControl, ReadDataByIdentifier, ReadDTCInformation

from common.diagnostic_schema import DEFAULT_SIMULATED_VIN, DID_CODECS, DID_ENGINE_RPM, DID_VEHICLE_SPEED_KPH
from common.telemetry_schema import DEFAULT_VEHICLE_ID
from simulation.uds.uds_server import PowertrainUDSServer


class _FakePowertrainECU:
    """Stand-in for PowertrainECU exposing just the read-only properties
    the UDS server needs, so these tests don't depend on a running ECU or
    its random-walk drift."""

    def __init__(self, speed_kph: float, rpm: float) -> None:
        self.speed_kph = speed_kph
        self.rpm = rpm


@pytest.fixture
def server() -> PowertrainUDSServer:
    return PowertrainUDSServer(
        session_id="test-session", vehicle_id=DEFAULT_VEHICLE_ID,
        powertrain_ecu=_FakePowertrainECU(speed_kph=100.0, rpm=3000.0),
    )


# --- DiagnosticSessionControl (0x10) ---

def test_session_control_extended_session_is_positive(server):
    request = DiagnosticSessionControl.make_request(
        DiagnosticSessionControl.Session.extendedDiagnosticSession
    )
    response_payload, event = server.handle_request(request.get_payload())

    assert response_payload[0] == 0x50  # positive response SID
    assert response_payload[1] == DiagnosticSessionControl.Session.extendedDiagnosticSession
    assert event.is_positive_response is True
    assert event.service_id == 0x10
    assert event.negative_response_code is None


def test_session_control_unsupported_session_is_negative(server):
    request = DiagnosticSessionControl.make_request(0x7F)  # not a session this server supports
    response_payload, event = server.handle_request(request.get_payload())

    assert response_payload[0] == 0x7F  # negative response marker
    assert event.is_positive_response is False
    assert event.negative_response_code == "SubFunctionNotSupported"


# --- ReadDataByIdentifier (0x22) ---

def test_read_vin_returns_synthetic_vin_not_vehicle_id(server):
    request = ReadDataByIdentifier.make_request(didlist=[0xF190], didconfig=DID_CODECS)
    response_payload, event = server.handle_request(request.get_payload())

    assert response_payload[0] == 0x62  # positive response SID
    decoded_vin = DID_CODECS[0xF190].decode(response_payload[3:])
    assert decoded_vin == DEFAULT_SIMULATED_VIN
    assert decoded_vin != DEFAULT_VEHICLE_ID
    assert event.is_positive_response is True


def test_read_live_speed_reflects_ecu_state(server):
    request = ReadDataByIdentifier.make_request(didlist=[DID_VEHICLE_SPEED_KPH], didconfig=DID_CODECS)
    response_payload, event = server.handle_request(request.get_payload())

    decoded_speed = DID_CODECS[DID_VEHICLE_SPEED_KPH].decode(response_payload[3:])
    assert decoded_speed == pytest.approx(100.0, abs=0.1)
    assert event.is_positive_response is True


def test_read_live_rpm_reflects_ecu_state(server):
    request = ReadDataByIdentifier.make_request(didlist=[DID_ENGINE_RPM], didconfig=DID_CODECS)
    response_payload, event = server.handle_request(request.get_payload())

    decoded_rpm = DID_CODECS[DID_ENGINE_RPM].decode(response_payload[3:])
    assert decoded_rpm == pytest.approx(3000.0, abs=1.0)


def test_read_unsupported_did_is_negative(server):
    # 0x1234 is not one of the 3 DIDs this server supports.
    request = ReadDataByIdentifier.make_request(didlist=[0xF190], didconfig=DID_CODECS)
    payload = bytearray(request.get_payload())
    payload[1:3] = (0x1234).to_bytes(2, "big")  # tamper with the DID after building
    response_payload, event = server.handle_request(bytes(payload))

    assert response_payload[0] == 0x7F
    assert event.is_positive_response is False
    assert event.negative_response_code == "RequestOutOfRange"


def test_read_live_did_without_ecu_reference_is_negative():
    server_without_ecu = PowertrainUDSServer(session_id="test-session", powertrain_ecu=None)
    request = ReadDataByIdentifier.make_request(didlist=[DID_VEHICLE_SPEED_KPH], didconfig=DID_CODECS)
    response_payload, event = server_without_ecu.handle_request(request.get_payload())

    assert response_payload[0] == 0x7F
    assert event.negative_response_code == "RequestOutOfRange"


# --- ReadDTCInformation (0x19) ---

def test_read_dtc_by_status_mask_returns_both_static_dtcs(server):
    request = ReadDTCInformation.make_request(
        subfunction=ReadDTCInformation.Subfunction.reportDTCByStatusMask, status_mask=0xFF
    )
    response_payload, event = server.handle_request(request.get_payload())

    assert response_payload[0] == 0x59  # positive response SID
    # payload: [SID, subfunction, availability_mask, then 4 bytes per DTC]
    dtc_records = response_payload[3:]
    assert len(dtc_records) == 2 * 4  # 2 static DTCs, 4 bytes each
    assert event.is_positive_response is True
    assert "P0217" in event.response_summary and "P0420" in event.response_summary


def test_read_dtc_unsupported_subfunction_is_negative(server):
    request = ReadDTCInformation.make_request(
        subfunction=ReadDTCInformation.Subfunction.reportSupportedDTCs
    )
    response_payload, event = server.handle_request(request.get_payload())

    assert response_payload[0] == 0x7F
    assert event.negative_response_code == "SubFunctionNotSupported"
