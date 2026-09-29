"""
Broker-backed dashboard integration tests -- the real chain, nothing mocked:

  real simulated ECUs -> VirtualBus -> EdgeGateway -> MQTT (local Mosquitto)
    -> dashboard MQTT adapter -> dashboard HTTP API

Skipped without a broker; failed under MQTT_BROKER_REQUIRED=1 (see
edge_gateway/tests/conftest.py).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from dashboard.backend.server import Dashboard
from edge_gateway.buffer import TelemetryBuffer
from edge_gateway.gateway import EdgeGateway
from edge_gateway.mqtt_publisher import MqttPublisher
from simulation.can_bus import get_bus, run_ecu
from simulation.ecus.battery_ecu import BatteryECU
from simulation.ecus.body_ecu import BodyECU
from simulation.ecus.powertrain_ecu import PowertrainECU


@pytest.fixture
def dashboard(mosquitto_broker, mosquitto_host):
    d = Dashboard(port=0, mqtt_host=mosquitto_host, mqtt_port=mosquitto_broker,
                  scripted_overrides=dict(normal_events=10, outage_events=20, recovery_events=10))
    d.start()
    yield d
    d.stop()


def _state(d, vehicle=None):
    q = f"?vehicle={vehicle}" if vehicle else ""
    with urllib.request.urlopen(d.url + "/api/state" + q, timeout=5) as res:
        return json.load(res)


def _post(d, action):
    req = urllib.request.Request(d.url + "/api/actions/" + action, method="POST", headers={"X-Dashboard-Action": "1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, **json.load(exc)}


def _wait(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return predicate()


def test_external_gateway_telemetry_reaches_the_dashboard_api(dashboard, mosquitto_broker, mosquitto_host, tmp_path):
    """A gateway NOT hosted by the dashboard (wired like run_demo.py, in
    this test process): its real telemetry must appear in the dashboard's
    API with the gateway's session_id and event_ids intact."""
    assert _wait(lambda: _state(dashboard)["broker"]["state"] == "connected", 10)
    vehicle_id = f"DASH-IT-{int(time.time() * 1000) % 100000}"
    gateway_bus = get_bus()
    publisher = MqttPublisher(host=mosquitto_host, port=mosquitto_broker)
    publisher.connect()
    buffer = TelemetryBuffer(tmp_path / "b.db")
    gateway = EdgeGateway(gateway_bus, publisher, buffer, vehicle_id=vehicle_id)
    ecus = [PowertrainECU("it", vehicle_id, rng_seed=1), BatteryECU("it", vehicle_id, rng_seed=1),
            BodyECU("it", vehicle_id, rng_seed=1)]
    buses = [get_bus() for _ in ecus]
    stop = threading.Event()
    threads = [threading.Thread(target=gateway.run, args=(stop,), daemon=True)] + [
        threading.Thread(target=run_ecu, args=(e, b, stop, e.TICK_INTERVAL_SECONDS), daemon=True)
        for e, b in zip(ecus, buses)]
    try:
        for t in threads:
            t.start()
        snap = _wait(lambda: (lambda s: s if any(v["vehicle_id"] == vehicle_id and len(v["ecus"]) == 3
                                                 for v in s["vehicles"]) else None)(_state(dashboard, vehicle_id)))
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
        for b in buses + [gateway_bus]:
            b.shutdown()
        publisher.disconnect()
        buffer.close()

    assert snap, "telemetry from the external gateway never reached the dashboard"
    vehicle = next(v for v in snap["vehicles"] if v["vehicle_id"] == vehicle_id)
    assert set(vehicle["ecus"]) == {"powertrain", "battery", "body"}
    assert vehicle["last_session_id"] == gateway.session_id
    ours = [e for e in snap["events"] if e["vehicle_id"] == vehicle_id]
    assert ours and all(e["session_id"] == gateway.session_id for e in ours)
    assert len({e["event_id"] for e in ours}) == len(ours)
    assert snap["selected_vehicle"] == vehicle_id and "vehicle_speed_kph" in snap["series"]
    assert snap["telemetry"]["flow"] in ("flowing", "stale")
    # Not hosted by the dashboard -> no invented gateway metrics for it.
    assert snap["gateway"]["live"]["running"] is False


def test_hosted_live_demo_outage_and_recovery_are_visible_through_the_api(dashboard):
    assert _post(dashboard, "live/start")["ok"]
    live = _wait(lambda: (lambda l: l if l["metrics"]["processed"] >= 20 else None)(_state(dashboard)["gateway"]["live"]))
    session_id, vehicle_id = live["session_id"], live["vehicle_id"]
    assert live["resilience"]["state"] == "NORMAL" and live["mqtt_connected"] is True

    snap = _wait(lambda: (lambda s: s if any(e["vehicle_id"] == vehicle_id for e in s["events"]) else None)(_state(dashboard)))
    assert all(e["session_id"] == session_id for e in snap["events"] if e["vehicle_id"] == vehicle_id)

    assert _post(dashboard, "outage/inject")["ok"]
    live = _wait(lambda: (lambda l: l if l["resilience"]["state"] == "BUFFERING" and l["buffer_depth"] >= 10 else None)(
        _state(dashboard)["gateway"]["live"]))
    assert live, "never observed BUFFERING with rows in SQLite"
    assert live["mqtt_connected"] is False and live["metrics"]["buffered"] >= live["buffer_depth"]

    assert _post(dashboard, "outage/clear")["ok"]
    live = _wait(lambda: (lambda l: l if l["resilience"]["state"] == "RECOVERED" else None)(
        _state(dashboard)["gateway"]["live"]), timeout=45)  # > the 30 s backoff cap
    assert live, "never observed RECOVERED"
    m = live["metrics"]
    assert live["buffer_depth"] == 0 and m["replayed"] == m["buffered"] == m["publish_failures"] > 0
    assert m["session_id"] == session_id and m["dropped"] == 0

    snap = _wait(lambda: (lambda s: s if any(e["status"] == "replayed" for e in s["events"]) or s["last_replay"] else None)(
        _state(dashboard)))
    assert snap["last_replay"]["session_id"] == session_id

    assert _post(dashboard, "live/stop")["ok"]
    snap = _wait(lambda: (lambda s: s if s["last_stopped_summary"] else None)(_state(dashboard)))
    assert snap["last_stopped_summary"]["session_id"] == session_id
    assert snap["gateway"]["last_run"]["metrics"]["session_id"] == session_id
    assert snap["gateway"]["live"]["resilience"]["state"] == "IDLE"


def test_scripted_resilience_run_is_reported_and_exclusive(dashboard):
    assert _post(dashboard, "resilience/run")["ok"]
    # Both runs share the process-local VirtualBus, so the live demo is refused meanwhile.
    refused = _post(dashboard, "live/start")
    assert refused["status"] == 409 and "resilience run" in refused["error"]
    dashboard.controller.wait_for_background(60)
    scripted = _state(dashboard)["gateway"]["scripted"]
    assert scripted["status"] == "passed", scripted.get("failure_reason")
    assert scripted["fifo_replay_verified"] and scripted["final_buffer_depth"] == 0
    messages = [a["message"] for a in _state(dashboard)["activity"] if a["source"] == "resilience_demo"]
    assert "scripted resilience run PASSED" in messages
    assert any(m.startswith("[5/6] Replaying buffered telemetry") for m in messages)  # the real stage output
