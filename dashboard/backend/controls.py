"""
Demo controls -- a fixed, small set of known actions, each of which only
*starts, stops, or drives existing components*. Nothing here implements
buffering, reconnect, replay, publishing, simulation, or diagnostics:

- live demo       the run_demo.py wiring (3 ECUs + EdgeGateway + SQLite
                  TelemetryBuffer + MqttPublisher to local Mosquitto) hosted
                  in this process -- the only way to read the gateway's real
                  GatewayMetrics, since the VirtualBus is process-local;
- outage          the existing edge_gateway.fault_injection.
                  simulated_connection_outage(), entered/exited on request;
- resilience run  the existing scenarios.resilience_demo.run_scenario();
- diagnostics     the existing UDS client/server over the VirtualBus plus
                  the deterministic DiagnosticAnalyzer (no LLM call).

At most one gateway run (live demo or scripted resilience run) at a time:
both share the process-local VirtualBus channel, and the scripted run's
verification reads every "edge_gateway" log record.

Local Mosquitto only: MqttPublisher is built directly (not via
cloud_publisher.build_publisher_from_env), so AWS_IOT_* is ignored.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import struct
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from udsoncan import Request
from udsoncan.exceptions import NegativeResponseException
from udsoncan.services import ReadDataByIdentifier

from analyzer.analyzer import DiagnosticAnalyzer
from common.diagnostic_schema import DID_ENGINE_RPM, DID_VEHICLE_SPEED_KPH, DiagnosticEvent
from dashboard.backend.sources import read_buffer_depth
from dashboard.backend.state import DashboardState, derive_resilience_state
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.fault_injection import simulated_connection_outage
from edge_gateway.gateway import EdgeGateway
from edge_gateway.mqtt_publisher import MqttPublisher
from scenarios.resilience_demo import DemoConfig, run_scenario
from simulation.can_bus import get_bus, run_ecu
from simulation.ecus.battery_ecu import BatteryECU
from simulation.ecus.body_ecu import BodyECU
from simulation.ecus.powertrain_ecu import PowertrainECU
from simulation.uds.uds_client import UDSTester
from simulation.uds.uds_server import PowertrainUDSServer, make_server_stack, run_server

THREAD_JOIN_TIMEOUT_SECONDS = 5.0
DID_VIN = 0xF190
UNSUPPORTED_DID = 0x1234

ACTIONS = ("live/start", "live/stop", "outage/inject", "outage/clear", "resilience/run", "diagnostics/run")


class ActionError(Exception):
    """An action that can't run right now (e.g. already running)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _LiveRun:
    """One hosted gateway run, wired exactly like run_demo.py."""

    def __init__(self, mqtt_host: str, mqtt_port: int) -> None:
        self.vehicle_id = f"DASH-DEMO-{uuid.uuid4().hex[:6].upper()}"
        self.started_at = time.time()
        self._temp_dir = tempfile.mkdtemp(prefix="dashboard-demo-")
        self.buffer_path = Path(self._temp_dir) / "buffer.db"
        self._stop = threading.Event()
        self._cleanup = contextlib.ExitStack()
        try:
            self._cleanup.callback(shutil.rmtree, self._temp_dir, ignore_errors=True)
            gateway_bus = get_bus()
            self._cleanup.callback(gateway_bus.shutdown)
            self.publisher = MqttPublisher(host=mqtt_host, port=mqtt_port,
                                           logger=logging.getLogger("edge_gateway.mqtt_publisher"))
            self.publisher.connect()  # raises OSError if the broker is down -- reported to the caller
            self._cleanup.callback(self.publisher.disconnect)
            self.buffer = TelemetryBuffer(self.buffer_path, logger=logging.getLogger("edge_gateway.buffer_store"))
            self._cleanup.callback(self.buffer.close)
            self.gateway = EdgeGateway(gateway_bus, self.publisher, self.buffer, vehicle_id=self.vehicle_id)
            self.powertrain = PowertrainECU("dashboard-demo-ecus", self.vehicle_id)
            ecus = [self.powertrain, BatteryECU("dashboard-demo-ecus", self.vehicle_id),
                    BodyECU("dashboard-demo-ecus", self.vehicle_id)]
            buses = [get_bus() for _ in ecus]
            for bus in buses:
                self._cleanup.callback(bus.shutdown)
            self._threads = [threading.Thread(target=self.gateway.run, args=(self._stop,), daemon=True, name="dash-gateway")]
            self._threads += [threading.Thread(target=run_ecu, args=(ecu, bus, self._stop, ecu.TICK_INTERVAL_SECONDS),
                                               daemon=True, name=f"dash-{type(ecu).__name__}")
                              for ecu, bus in zip(ecus, buses)]
            self._cleanup.callback(self._join)
            for thread in self._threads:
                thread.start()
        except BaseException:
            self._cleanup.close()
            raise
        self.outage_stack: Optional[contextlib.ExitStack] = None
        self.outage_cleared_pending = False

    def _join(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

    def stop(self) -> Dict[str, Any]:
        if self.outage_stack is not None:
            self.outage_stack.close()
            self.outage_stack = None
        self._join()
        final = self.gateway.metrics_snapshot().as_log_fields()
        final["buffer_pending"] = read_buffer_depth(self.buffer_path)
        self._cleanup.close()  # LIFO: threads joined, buses shut, buffer closed, publisher disconnected, temp dir removed
        return final


class DemoController:
    def __init__(self, state: DashboardState, mqtt_host: str, mqtt_port: int,
                 external_buffer: Optional[str] = None,
                 scripted_overrides: Optional[Dict[str, Any]] = None) -> None:
        self._state = state
        self._mqtt_host, self._mqtt_port = mqtt_host, mqtt_port
        self._external_buffer = external_buffer
        self._scripted_overrides = dict(scripted_overrides or {})
        self._lock = threading.RLock()
        self._live: Optional[_LiveRun] = None
        self._last_run: Optional[Dict[str, Any]] = None
        self._scripted: Dict[str, Any] = {"status": "not run"}
        self._scripted_thread: Optional[threading.Thread] = None
        self._diagnostics_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ actions

    def perform(self, action: str) -> str:
        handlers: Dict[str, Callable[[], str]] = {
            "live/start": self.start_live, "live/stop": self.stop_live,
            "outage/inject": self.inject_outage, "outage/clear": self.clear_outage,
            "resilience/run": self.start_resilience_run, "diagnostics/run": self.start_diagnostics,
        }
        if action not in handlers:
            raise KeyError(action)
        return handlers[action]()

    def start_live(self) -> str:
        with self._lock:
            if self._live is not None:
                raise ActionError("the live demo is already running")
            if self._scripted_running():
                raise ActionError("a scripted resilience run is in progress")
            try:
                self._live = _LiveRun(self._mqtt_host, self._mqtt_port)
            except OSError as exc:
                raise ActionError(f"cannot connect the gateway to MQTT at {self._mqtt_host}:{self._mqtt_port} ({exc})")
            self._state.add_activity("dashboard", "INFO", "live demo started", {
                "session_id": self._live.gateway.session_id, "vehicle_id": self._live.vehicle_id})
            return f"live demo started (vehicle {self._live.vehicle_id})"

    def stop_live(self) -> str:
        with self._lock:
            if self._live is None:
                raise ActionError("the live demo is not running")
            live, self._live = self._live, None
            final = live.stop()
            self._last_run = {"vehicle_id": live.vehicle_id, "stopped_at": _now_iso(), "metrics": final}
            self._state.add_activity("dashboard", "INFO", "live demo stopped", {"session_id": final["session_id"]})
            return "live demo stopped"

    def inject_outage(self) -> str:
        with self._lock:
            live = self._require_live()
            if live.outage_stack is not None:
                raise ActionError("an outage is already injected")
            live.outage_stack = contextlib.ExitStack()
            live.outage_stack.enter_context(simulated_connection_outage(live.publisher))
            live.outage_cleared_pending = False
            self._state.add_activity("dashboard", "WARNING", "simulated MQTT connection outage injected",
                                     {"session_id": live.gateway.session_id})
            return "simulated MQTT outage injected"

    def clear_outage(self) -> str:
        with self._lock:
            live = self._require_live()
            if live.outage_stack is None:
                raise ActionError("no outage is injected")
            live.outage_stack.close()
            live.outage_stack = None
            live.outage_cleared_pending = True
            self._state.add_activity("dashboard", "INFO", "simulated outage cleared; waiting for the gateway's reconnect",
                                     {"session_id": live.gateway.session_id})
            return "outage cleared -- the gateway reconnects on its next backoff-gated attempt"

    def start_resilience_run(self) -> str:
        with self._lock:
            if self._live is not None:
                raise ActionError("stop the live demo first (both runs share the virtual CAN bus)")
            if self._scripted_running():
                raise ActionError("a scripted resilience run is already in progress")
            self._scripted = {"status": "running", "started_at": _now_iso()}
            self._scripted_thread = threading.Thread(target=self._run_scripted, daemon=True, name="dash-resilience")
            self._scripted_thread.start()
            return "scripted resilience run started"

    def start_diagnostics(self) -> str:
        with self._lock:
            if self._diagnostics_thread is not None and self._diagnostics_thread.is_alive():
                raise ActionError("a diagnostic session is already running")
            powertrain = self._live.powertrain if self._live is not None else None
            vehicle_id = self._live.vehicle_id if self._live is not None else "SIM-VEHICLE-01"
            self._state.set_diagnostics({"status": "running", "started_at": _now_iso(), "events": [], "anomalies": []})
            self._diagnostics_thread = threading.Thread(
                target=self._run_diagnostics, args=(powertrain, vehicle_id), daemon=True, name="dash-uds")
            self._diagnostics_thread.start()
            return "UDS diagnostic session started"

    def shutdown(self) -> None:
        with self._lock:
            if self._live is not None:
                self.stop_live()
        for thread in (self._scripted_thread, self._diagnostics_thread):
            if thread is not None:
                thread.join(timeout=60)

    def wait_for_background(self, timeout: float = 60.0) -> None:
        """Test helper: wait for a scripted run / diagnostic session to finish."""
        for thread in (self._scripted_thread, self._diagnostics_thread):
            if thread is not None:
                thread.join(timeout=timeout)

    # ------------------------------------------------------------------ status

    def status(self) -> Dict[str, Any]:
        with self._lock:
            live = self._live
            status: Dict[str, Any] = {
                "mode": "live" if live else ("scripted" if self._scripted_running() else "idle"),
                "actions": list(ACTIONS),
                "last_run": self._last_run,
                "scripted": dict(self._scripted),
                "diagnostics_running": bool(self._diagnostics_thread and self._diagnostics_thread.is_alive()),
                "external_buffer": None,
            }
            if self._external_buffer:
                status["external_buffer"] = {"path": self._external_buffer,
                                             "depth": read_buffer_depth(self._external_buffer)}
            if live is None:
                status["live"] = {"running": False,
                                  "resilience": derive_resilience_state(running=False, mqtt_connected=None, outage="none",
                                                                        buffer_depth=None, replayed_total=0,
                                                                        seconds_since_replay=None)}
                return status

            connected = live.publisher.is_connected()
            if live.outage_cleared_pending and connected:
                live.outage_cleared_pending = False
            outage = "active" if live.outage_stack is not None else ("cleared" if live.outage_cleared_pending else "none")
            depth = read_buffer_depth(live.buffer_path)
            metrics = live.gateway.metrics_snapshot().as_log_fields()
            status["live"] = {
                "running": True, "session_id": live.gateway.session_id, "vehicle_id": live.vehicle_id,
                "started_at": datetime.fromtimestamp(live.started_at, tz=timezone.utc).isoformat(),
                "uptime_seconds": round(time.time() - live.started_at, 1),
                "mqtt_connected": connected, "outage": outage, "broker": f"{self._mqtt_host}:{self._mqtt_port}",
                "metrics": metrics, "buffer_depth": depth,
                "resilience": derive_resilience_state(
                    running=True, mqtt_connected=connected, outage=outage, buffer_depth=depth,
                    replayed_total=metrics["replayed"], seconds_since_replay=self._state.last_replay_age()),
            }
            return status

    # ------------------------------------------------------------------ helpers

    def _require_live(self) -> _LiveRun:
        if self._live is None:
            raise ActionError("start the live demo first")
        return self._live

    def _scripted_running(self) -> bool:
        return self._scripted_thread is not None and self._scripted_thread.is_alive()

    def _run_scripted(self) -> None:
        def announce(line: str) -> None:
            text = line.strip()
            if text and set(text) != {"="}:
                self._state.add_activity("resilience_demo", "INFO", text)

        config = DemoConfig(mqtt_host=self._mqtt_host, mqtt_port=self._mqtt_port, announce=announce,
                            **self._scripted_overrides)
        result = run_scenario(config)
        fields = result.summary_fields()
        self._scripted = {"status": "passed" if result.passed else "failed", "finished_at": _now_iso(), **fields}
        self._state.add_activity("resilience_demo", "INFO" if result.passed else "ERROR",
                                 f"scripted resilience run {'PASSED' if result.passed else 'FAILED'}",
                                 {"session_id": result.session_id})

    def _run_diagnostics(self, powertrain, vehicle_id: str) -> None:
        started = _now_iso()
        try:
            events = run_uds_session(powertrain, vehicle_id)
            reports = DiagnosticAnalyzer().analyze(events)  # deterministic rules only; no LLM call
            self._state.set_diagnostics({
                "status": "completed", "started_at": started, "finished_at": _now_iso(),
                "vehicle_id": vehicle_id, "llm": "not requested (the LLM layer is optional and advisory; "
                                                 "the dashboard never calls it)",
                "events": [e.model_dump(mode="json") for e in events],
                "anomalies": [r.model_dump(mode="json") for r in reports],
            })
            self._state.add_activity("diagnostics", "INFO",
                                     f"UDS session completed: {len(events)} diagnostic events, {len(reports)} rule findings")
        except Exception as exc:  # report, never crash the dashboard
            self._state.set_diagnostics({"status": "failed", "started_at": started, "error": f"{type(exc).__name__}: {exc}",
                                         "events": [], "anomalies": []})
            self._state.add_activity("diagnostics", "ERROR", f"UDS session failed: {type(exc).__name__}")


def run_uds_session(powertrain: Optional[PowertrainECU], vehicle_id: str, timeout: float = 10.0) -> List[DiagnosticEvent]:
    """A fixed, known UDS script over the VirtualBus using the existing
    PowertrainUDSServer and UDSTester: extended session, VIN, live speed and
    RPM, three DTC reads, and three reads of an unsupported DID (negative
    responses) -- enough to exercise all three deterministic analyzer rules.
    Returns the DiagnosticEvents the server emitted."""
    powertrain = powertrain or PowertrainECU("dashboard-uds", vehicle_id)
    diag_session = f"dashboard-uds-{uuid.uuid4().hex[:8]}"
    events: List[DiagnosticEvent] = []
    server_bus, client_bus = get_bus(), get_bus()
    stop = threading.Event()
    server = PowertrainUDSServer(session_id=diag_session, vehicle_id=vehicle_id, powertrain_ecu=powertrain)
    thread = threading.Thread(target=run_server, args=(server, make_server_stack(server_bus), stop, events.append),
                              daemon=True, name="dash-uds-server")
    thread.start()
    expected = 0
    try:
        with UDSTester(client_bus) as tester:
            tester.enter_extended_session()
            tester.read_did(DID_VIN)
            tester.read_did(DID_VEHICLE_SPEED_KPH)
            tester.read_did(DID_ENGINE_RPM)
            expected = 4
            for _ in range(3):
                tester.read_dtcs()
                expected += 1
            for _ in range(3):
                # Same raw-request approach as simulation/uds/tests/test_uds_integration.py:
                # udsoncan rejects unknown DIDs locally, so send the request directly
                # to get the server's real over-the-wire negative response.
                with contextlib.suppress(NegativeResponseException):
                    tester._client.send_request(Request(service=ReadDataByIdentifier,
                                                        data=struct.pack(">H", UNSUPPORTED_DID)))
                expected += 1
        deadline = time.monotonic() + timeout
        while len(events) < expected and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        client_bus.shutdown()
        server_bus.shutdown()
    return list(events)
