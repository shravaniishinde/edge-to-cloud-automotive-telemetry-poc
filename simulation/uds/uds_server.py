"""
Hand-rolled UDS server hosted by the Powertrain ECU (Phase 2).

`udsoncan` is a CLIENT-side library only -- it has no server
implementation. This module builds a small server directly on top of:
  - `isotp.CanStack` for ISO-TP segmentation/reassembly over the
    existing virtual CAN bus (confirmed working via the Phase 2 smoke
    test).
  - `udsoncan.Request` / `udsoncan.Response` for parsing incoming
    request bytes and building compliant response bytes, without
    reimplementing UDS's own byte-level framing.

Supports exactly 3 services, per the Phase 2 plan:
  0x10 DiagnosticSessionControl
  0x22 ReadDataByIdentifier
  0x19 ReadDTCInformation (subfunction reportDTCByStatusMask only)

Session state is tracked (see `_current_session`) but not yet enforced --
no service is gated behind having entered an extended session. This is a
deliberate Phase 2 limitation, documented in docs/uds-spec.md.
"""

from __future__ import annotations

import struct
import threading
from typing import Optional, Tuple

import can
import isotp
import udsoncan
from udsoncan.services import DiagnosticSessionControl, ReadDataByIdentifier, ReadDTCInformation

from common.diagnostic_schema import (
    DEFAULT_SIMULATED_VIN,
    DID_CODECS,
    DID_ENGINE_RPM,
    DID_VEHICLE_SPEED_KPH,
    DID_VIN,
    STATIC_DTCS,
    UDS_REQUEST_CAN_ID,
    UDS_RESPONSE_CAN_ID,
    DiagnosticEvent,
)
from common.telemetry_schema import DEFAULT_VEHICLE_ID, ECUSource

_SUPPORTED_SESSIONS = {
    DiagnosticSessionControl.Session.defaultSession,
    DiagnosticSessionControl.Session.extendedDiagnosticSession,
}

# Placeholder P2/P2* server timing values (P2=0x0032=50ms, P2*=0x01F4 in the
# standard's 10ms units for P2*). Not measured/tuned against any real
# timing budget -- they exist only because the session-control positive
# response format requires *something* here.
_SESSION_TIMING_BYTES = bytes([0x00, 0x32, 0x01, 0xF4])

_DTC_STATUS_BYTE = 0x08  # simplified static "status" for every reported DTC
_DTC_STATUS_AVAILABILITY_MASK = 0xFF


def make_server_stack(bus: "can.BusABC") -> isotp.CanStack:
    """Build the ECU-side ISO-TP stack: listens on the tester's request ID,
    transmits on the ECU's response ID."""
    address = isotp.Address(
        isotp.AddressingMode.Normal_11bits,
        rxid=UDS_REQUEST_CAN_ID,
        txid=UDS_RESPONSE_CAN_ID,
    )
    return isotp.CanStack(bus=bus, address=address)


class PowertrainUDSServer:
    """
    UDS server hosted by the (simulated) Powertrain ECU. Owns UDS request
    handling only -- it does not generate telemetry; pass in the running
    `PowertrainECU` instance if live DIDs (speed/RPM) should reflect real
    simulated state, or omit it to run the server standalone (those two
    DIDs then respond with a negative response, since there is no state to
    report).
    """

    def __init__(
        self,
        session_id: str,
        vehicle_id: str = DEFAULT_VEHICLE_ID,
        powertrain_ecu: Optional[object] = None,
    ) -> None:
        self._session_id = session_id
        self._vehicle_id = vehicle_id
        self._powertrain_ecu = powertrain_ecu
        self._current_session = DiagnosticSessionControl.Session.defaultSession

    def handle_request(self, payload: bytes) -> Tuple[bytes, DiagnosticEvent]:
        """Parse one raw UDS request payload (already ISO-TP-reassembled)
        and return (raw response payload, DiagnosticEvent)."""
        req = udsoncan.Request.from_payload(payload)

        if req.service is None:
            # Payload didn't even map to a known service ID. There's no
            # `service` to build a compliant udsoncan.Response against, so
            # this is simply dropped -- a documented Phase 2 limitation
            # rather than a full generalNotSupported implementation.
            raise ValueError(f"Unrecognized UDS request payload: {payload!r}")

        if req.service == DiagnosticSessionControl:
            return self._handle_session_control(req)
        if req.service == ReadDataByIdentifier:
            return self._handle_read_did(req)
        if req.service == ReadDTCInformation:
            return self._handle_read_dtc(req)

        response = udsoncan.Response(
            service=req.service, code=udsoncan.Response.Code.ServiceNotSupported
        )
        event = self._make_event(
            req.service,
            request_summary="(unsupported service)",
            response_summary="serviceNotSupported",
            is_positive=False,
            negative_response_code="ServiceNotSupported",
        )
        return response.get_payload(), event

    def _handle_session_control(self, req: "udsoncan.Request") -> Tuple[bytes, DiagnosticEvent]:
        requested_session = req.subfunction
        if requested_session in _SUPPORTED_SESSIONS:
            self._current_session = requested_session
            session_name = DiagnosticSessionControl.Session.get_name(requested_session)
            response = udsoncan.Response(
                service=DiagnosticSessionControl,
                code=udsoncan.Response.Code.PositiveResponse,
                data=bytes([requested_session]) + _SESSION_TIMING_BYTES,
            )
            event = self._make_event(
                DiagnosticSessionControl,
                request_summary=f"requested_session={session_name}",
                response_summary=f"session_active={session_name}",
                is_positive=True,
            )
        else:
            response = udsoncan.Response(
                service=DiagnosticSessionControl,
                code=udsoncan.Response.Code.SubFunctionNotSupported,
            )
            event = self._make_event(
                DiagnosticSessionControl,
                request_summary=f"requested_session=0x{requested_session:02X}",
                response_summary="subFunctionNotSupported",
                is_positive=False,
                negative_response_code="SubFunctionNotSupported",
            )
        return response.get_payload(), event

    def _handle_read_did(self, req: "udsoncan.Request") -> Tuple[bytes, DiagnosticEvent]:
        if not req.data or len(req.data) < 2:
            response = udsoncan.Response(
                service=ReadDataByIdentifier, code=udsoncan.Response.Code.IncorrectMessageLengthOrInvalidFormat
            )
            event = self._make_event(
                ReadDataByIdentifier,
                request_summary="(malformed request)",
                response_summary="incorrectMessageLengthOrInvalidFormat",
                is_positive=False,
                negative_response_code="IncorrectMessageLengthOrInvalidFormat",
            )
            return response.get_payload(), event

        (did,) = struct.unpack(">H", req.data[:2])
        value = self._read_live_did_value(did)

        if did not in DID_CODECS or value is None:
            response = udsoncan.Response(
                service=ReadDataByIdentifier, code=udsoncan.Response.Code.RequestOutOfRange
            )
            event = self._make_event(
                ReadDataByIdentifier,
                request_summary=f"DID=0x{did:04X}",
                response_summary="requestOutOfRange (unsupported DID)",
                is_positive=False,
                negative_response_code="RequestOutOfRange",
            )
            return response.get_payload(), event

        encoded_value = DID_CODECS[did].encode(value)
        response = udsoncan.Response(
            service=ReadDataByIdentifier,
            code=udsoncan.Response.Code.PositiveResponse,
            data=struct.pack(">H", did) + encoded_value,
        )
        event = self._make_event(
            ReadDataByIdentifier,
            request_summary=f"DID=0x{did:04X}",
            response_summary=f"DID=0x{did:04X} value={value!r}",
            is_positive=True,
        )
        return response.get_payload(), event

    def _read_live_did_value(self, did: int):
        """Returns the value to encode for a supported DID, or None if the
        DID is unsupported or its backing state isn't available."""
        if did == DID_VIN:
            return DEFAULT_SIMULATED_VIN
        if did == DID_VEHICLE_SPEED_KPH:
            return self._powertrain_ecu.speed_kph if self._powertrain_ecu is not None else None
        if did == DID_ENGINE_RPM:
            return self._powertrain_ecu.rpm if self._powertrain_ecu is not None else None
        return None

    def _handle_read_dtc(self, req: "udsoncan.Request") -> Tuple[bytes, DiagnosticEvent]:
        if req.subfunction != ReadDTCInformation.Subfunction.reportDTCByStatusMask:
            response = udsoncan.Response(
                service=ReadDTCInformation, code=udsoncan.Response.Code.SubFunctionNotSupported
            )
            event = self._make_event(
                ReadDTCInformation,
                request_summary=f"subfunction=0x{req.subfunction:02X}",
                response_summary="subFunctionNotSupported",
                is_positive=False,
                negative_response_code="SubFunctionNotSupported",
            )
            return response.get_payload(), event

        # POC simplification: the requested status_mask (req.data[0], if
        # present) is accepted but NOT used to filter -- this server
        # always reports its full static DTC list. A real server would AND
        # the mask against each DTC's actual status byte. See
        # docs/uds-spec.md.
        records = b"".join(
            ReadDTCInformation.pack_dtc(code) + bytes([_DTC_STATUS_BYTE]) for code, _label in STATIC_DTCS
        )
        response = udsoncan.Response(
            service=ReadDTCInformation,
            code=udsoncan.Response.Code.PositiveResponse,
            data=bytes([req.subfunction, _DTC_STATUS_AVAILABILITY_MASK]) + records,
        )
        labels = [label for _code, label in STATIC_DTCS]
        event = self._make_event(
            ReadDTCInformation,
            request_summary="subfunction=reportDTCByStatusMask",
            response_summary=f"returned {len(STATIC_DTCS)} static DTC(s): {labels}",
            is_positive=True,
        )
        return response.get_payload(), event

    def _make_event(
        self,
        service,
        *,
        request_summary: str,
        response_summary: str,
        is_positive: bool,
        negative_response_code: Optional[str] = None,
    ) -> DiagnosticEvent:
        return DiagnosticEvent(
            session_id=self._session_id,
            vehicle_id=self._vehicle_id,
            source_ecu=ECUSource.POWERTRAIN,
            service_id=service.request_id(),
            service_name=service.get_name(),
            request_summary=request_summary,
            response_summary=response_summary,
            is_positive_response=is_positive,
            negative_response_code=negative_response_code,
        )


def run_server(
    server: PowertrainUDSServer,
    stack: isotp.CanStack,
    stop_event: threading.Event,
    on_event=None,
) -> None:
    """Blocking loop: start the ISO-TP stack, service requests until
    `stop_event` is set. `on_event`, if given, is called with each
    DiagnosticEvent as it's produced (e.g. for logging)."""
    stack.start()
    try:
        while not stop_event.is_set():
            payload = stack.recv(block=True, timeout=0.2)
            if payload is None:
                continue
            response_payload, event = server.handle_request(payload)
            stack.send(response_payload)
            if on_event is not None:
                on_event(event)
    finally:
        stack.stop()
