"""HTTP/API tests against a real running dashboard server. The dashboard
is pointed at a port with no broker, which doubles as the
"broker unavailable" test: the server must keep serving useful state."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from dashboard.backend import server as server_module
from dashboard.backend.controls import run_uds_session
from dashboard.backend.server import Dashboard


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def dashboard():
    d = Dashboard(port=0, mqtt_host="127.0.0.1", mqtt_port=_unused_port())
    d.start()
    yield d
    d.stop()


def _get(d, path):
    with urllib.request.urlopen(d.url + path, timeout=5) as res:
        return res.status, dict(res.headers), res.read()


def _post(d, action, header=True):
    req = urllib.request.Request(d.url + "/api/actions/" + action, method="POST",
                                 headers={"X-Dashboard-Action": "1"} if header else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_module_imports_and_serves_the_ui(dashboard):
    status, headers, body = _get(dashboard, "/")
    assert status == 200 and b"Telemetry Engineering Dashboard" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    for name in server_module.STATIC_FILES:
        assert _get(dashboard, "/static/" + name)[0] == 200


def test_a_second_dashboard_cannot_silently_share_the_port(dashboard):
    """Audit regression: on Windows, http.server's SO_REUSEADDR let a second
    dashboard bind a port already in use, and requests then went to either
    process nondeterministically."""
    port = dashboard.httpd.server_address[1]
    with pytest.raises(OSError):
        Dashboard(port=port, mqtt_host="127.0.0.1", mqtt_port=_unused_port())


def test_cli_reports_a_busy_port_instead_of_crashing(dashboard, capsys):
    import logging

    from dashboard.backend.__main__ import main

    gateway_logger, root = logging.getLogger("edge_gateway"), logging.getLogger()
    before = (gateway_logger.propagate, list(root.handlers))
    assert main(["--port", str(dashboard.httpd.server_address[1])]) == 2
    assert "Cannot listen on" in capsys.readouterr().err
    assert (gateway_logger.propagate, list(root.handlers)) == before  # a failed start leaves logging untouched


def test_only_allowlisted_static_files_are_served(dashboard):
    for path in ("/static/../backend/server.py", "/static/index.html", "/static/%2e%2e/%2e%2e/conftest.py", "/nope"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(dashboard, path)
        assert exc.value.code == 404


def test_state_endpoint_reports_broker_unavailable_and_empty_state(dashboard):
    deadline = time.monotonic() + 5
    while True:
        snap = json.loads(_get(dashboard, "/api/state")[2])
        if snap["broker"]["state"] == "disconnected" or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert snap["broker"]["state"] == "disconnected"
    assert snap["telemetry"]["flow"] == "none"
    assert snap["gateway"]["mode"] == "idle"
    assert snap["gateway"]["live"]["resilience"]["state"] == "IDLE"
    assert snap["gateway"]["live"]["running"] is False


def test_stream_sends_json_snapshots(dashboard):
    with urllib.request.urlopen(dashboard.url + "/api/stream", timeout=5) as res:
        assert res.headers["Content-Type"] == "text/event-stream"
        line = res.readline().decode()
    assert line.startswith("data: ")
    assert "telemetry" in json.loads(line[len("data: "):])


def test_actions_require_the_header_and_a_known_name(dashboard):
    assert _post(dashboard, "live/start", header=False)[0] == 403
    assert _post(dashboard, "rm%20-rf")[0] == 404
    assert _post(dashboard, "../../etc")[0] == 404


def test_actions_fail_cleanly_when_the_broker_is_down(dashboard):
    status, body = _post(dashboard, "live/start")
    assert status == 409 and "cannot connect the gateway to MQTT" in body["error"]
    assert _post(dashboard, "outage/inject")[0] == 409  # nothing running
    assert json.loads(_get(dashboard, "/api/state")[2])["gateway"]["live"]["running"] is False


def test_diagnostics_use_the_real_uds_stack_and_deterministic_rules_only(dashboard):
    """UDS runs over the process-local VirtualBus -- no broker needed."""
    status, _body = _post(dashboard, "diagnostics/run")
    assert status == 200
    dashboard.controller.wait_for_background(30)

    diag = json.loads(_get(dashboard, "/api/state")[2])["diagnostics"]
    assert diag["status"] == "completed"
    assert len(diag["events"]) == 10
    assert {e["service_name"] for e in diag["events"]} >= {"ReadDataByIdentifier", "ReadDTCInformation"}
    assert sum(1 for e in diag["events"] if not e["is_positive_response"]) == 3
    assert {a["rule_id"] for a in diag["anomalies"]} == {
        "repeated_negative_responses", "repeated_dtc_queries", "repeated_p0217_activity"}
    assert all(a["llm_explanation"] is None for a in diag["anomalies"])  # the dashboard never calls the LLM
    assert "never calls it" in diag["llm"]


def test_uds_session_helper_returns_one_event_per_transaction():
    events = run_uds_session(None, "SIM-VEHICLE-01")
    assert len(events) == 10
    assert all(e.vehicle_id == "SIM-VEHICLE-01" for e in events)
