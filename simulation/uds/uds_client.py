"""
UDS diagnostic client/tester (Phase 2).

Plays the role of a technician's diagnostic tool: connects to the ECU's
UDS server over the same virtual CAN bus, using `udsoncan.client.Client`
on top of `isotp.CanStack` (wrapped in udsoncan's own
`PythonIsoTpConnection`) for the actual request/response mechanics.
"""

from __future__ import annotations

import can
import isotp
from udsoncan.client import Client
from udsoncan.connections import PythonIsoTpConnection
from udsoncan.services import DiagnosticSessionControl

from common.diagnostic_schema import DID_CODECS, UDS_REQUEST_CAN_ID, UDS_RESPONSE_CAN_ID


def make_client_stack(bus: "can.BusABC") -> isotp.CanStack:
    """Build the tester-side ISO-TP stack: transmits on the request ID,
    listens on the ECU's response ID -- the mirror image of
    uds_server.make_server_stack."""
    address = isotp.Address(
        isotp.AddressingMode.Normal_11bits,
        rxid=UDS_RESPONSE_CAN_ID,
        txid=UDS_REQUEST_CAN_ID,
    )
    return isotp.CanStack(bus=bus, address=address)


def make_client(stack: isotp.CanStack, request_timeout: float = 2.0) -> Client:
    """Build a ready-to-use udsoncan Client. Caller is responsible for
    opening/closing the connection (e.g. via `with client:`), matching
    udsoncan's own usage pattern."""
    connection = PythonIsoTpConnection(stack)
    return Client(
        connection,
        config={"data_identifiers": DID_CODECS},
        request_timeout=request_timeout,
    )


class UDSTester:
    """
    Thin convenience wrapper around `udsoncan.client.Client` exposing just
    the 3 services this project's server supports, so callers (tests, a
    future CLI) don't need to know udsoncan's lower-level call shapes.
    """

    def __init__(self, bus: "can.BusABC", request_timeout: float = 2.0) -> None:
        self._stack = make_client_stack(bus)
        self._client = make_client(self._stack, request_timeout=request_timeout)

    def __enter__(self) -> "UDSTester":
        self._client.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._client.__exit__(exc_type, exc_val, exc_tb)

    def change_session(self, session: int):
        return self._client.change_session(session)

    def read_did(self, did: int):
        """Returns the decoded value for a supported DID."""
        response = self._client.read_data_by_identifier(didlist=[did])
        return response.service_data.values[did]

    def read_dtcs(self, status_mask: int = 0xFF):
        """Returns the server's reported DTC records (status_mask is sent
        to the server, but this project's server does not filter by it --
        see docs/uds-spec.md)."""
        response = self._client.get_dtc_by_status_mask(status_mask=status_mask)
        return response.service_data.dtcs

    def enter_extended_session(self):
        return self.change_session(DiagnosticSessionControl.Session.extendedDiagnosticSession)
