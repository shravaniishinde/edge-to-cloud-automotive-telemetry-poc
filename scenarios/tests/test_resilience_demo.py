"""
Phase 9 tests for scenarios/resilience_demo.py.

The broker-backed tests run the REAL demo -- real ECUs, EdgeGateway,
SQLite buffer, MqttPublisher, and the real fault-injection scenario --
against local Mosquitto, just with smaller event counts. An independent
subscriber (Phase 8's `mqtt_subscriber` fixture) double-checks what the
demo's own observer reports. The Phase 4 buffer/replay mechanics
themselves are already covered in edge_gateway/tests/; these tests prove
the *scenario*: that it demonstrates what it claims, and that it fails
clearly and cleans up when it can't.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
import uuid
from pathlib import Path

from common.telemetry_schema import TelemetryEvent
from scenarios import resilience_demo
from scenarios.resilience_demo import DemoConfig, run_scenario

SMALL = dict(normal_events=10, outage_events=20, recovery_events=10)


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _gateway_logger_state():
    gateway_logger = logging.getLogger("edge_gateway")
    return gateway_logger.level, gateway_logger.propagate, list(gateway_logger.handlers)


def _announced_buffer_dir(lines):
    (line,) = [line for line in lines if "SQLite buffer" in line]
    return Path(line.split(":", 1)[1].strip()).parent


# --- no broker needed ---


def test_demo_module_imports_and_exposes_its_entry_points():
    assert callable(resilience_demo.main)
    assert callable(resilience_demo.run_scenario)
    assert DemoConfig().outage is resilience_demo.simulated_connection_outage  # the existing fault injection


def test_unreachable_broker_fails_fast_with_a_clear_message_and_cleans_up():
    logger_state_before = _gateway_logger_state()
    threads_before = threading.active_count()
    started = time.monotonic()

    result = run_scenario(DemoConfig(mqtt_port=_unused_port(), announce=lambda _: None, **SMALL))

    assert time.monotonic() - started < 10
    assert result.passed is False
    assert result.failed_stage == "setup"
    assert "cannot reach MQTT broker" in result.failure_reason
    assert result.cleanup_completed is True
    assert _gateway_logger_state() == logger_state_before  # logging config restored
    assert threading.active_count() <= threads_before


def test_main_returns_nonzero_when_the_scenario_fails(capsys):
    demo_logger = logging.getLogger("scenarios.resilience_demo")
    before = (demo_logger.level, demo_logger.propagate, list(demo_logger.handlers))

    exit_code = resilience_demo.main(["--mqtt-port", str(_unused_port())])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "RESULT: FAIL" in captured.out
    assert '"message": "resilience demo finished"' in captured.err  # the structured summary line
    assert (demo_logger.level, demo_logger.propagate, list(demo_logger.handlers)) == before  # no leakage


# --- broker-backed: the real scenario ---


def test_outage_buffering_recovery_and_fifo_replay_end_to_end(mosquitto_broker, mosquitto_host, mqtt_subscriber):
    vehicle_id = f"RD-TEST-{uuid.uuid4().hex[:8]}"
    independent = mqtt_subscriber(f"vehicle/{vehicle_id}/telemetry/#")
    lines = []

    result = run_scenario(DemoConfig(
        mqtt_host=mosquitto_host, mqtt_port=mosquitto_broker, vehicle_id=vehicle_id,
        announce=lines.append, **SMALL,
    ))

    assert result.passed, result.failure_reason
    # normal operation
    assert result.published_before_outage >= SMALL["normal_events"]
    # outage -> buffering (via the existing buffer, counted by the existing metrics)
    assert result.buffered_during_outage >= SMALL["outage_events"]
    assert result.max_buffer_depth == result.buffered_during_outage  # all of it sat in SQLite at once
    assert result.failed_reconnect_attempts >= 1  # the backoff path actually ran
    # recovery -> replay: every buffered event, in FIFO order, with its identity
    assert result.recovered is True
    assert result.replayed == result.replayed_received == result.buffered_during_outage
    assert result.fifo_replay_verified is True
    assert result.no_live_event_overtook_replay is True
    assert result.event_ids_preserved is True
    # back to normal, buffer drained
    assert result.live_received_after_recovery >= SMALL["recovery_events"]
    assert result.final_buffer_depth == 0 and result.buffer_drained is True
    assert result.duplicates_received == 0  # nothing in this controlled run can cause one
    # final metrics reflect the scenario
    assert result.metrics == {
        "session_id": result.session_id,
        "processed": result.published_before_outage + result.live_published_after_recovery,
        "rejected": 0,
        "publish_failures": result.buffered_during_outage,
        "buffered": result.buffered_during_outage,
        "replayed": result.buffered_during_outage,
        "dropped": 0,
    }
    # cleanup
    assert result.cleanup_completed is True
    assert not _announced_buffer_dir(lines).exists()  # temp buffer removed

    # Independent check, not relying on the demo's own observer: the
    # buffered event_ids reached a separate subscriber, per topic in FIFO
    # order, with the original payload identity.
    assert independent.wait_until(
        lambda msgs: set(result.buffered_event_ids) <= {TelemetryEvent.model_validate_json(m.payload).event_id for m in msgs},
    )
    received = [(m.topic, TelemetryEvent.model_validate_json(m.payload)) for m in independent.messages]
    by_id = {event.event_id: (topic, event) for topic, event in received}
    for topic in {by_id[i][0] for i in result.buffered_event_ids}:
        expected = [i for i in result.buffered_event_ids if by_id[i][0] == topic]
        on_topic = [event.event_id for t, event in received if t == topic and event.event_id in set(expected)]
        assert on_topic == expected
    assert all(by_id[i][1].session_id == result.session_id for i in result.buffered_event_ids)


def test_recovery_timeout_fails_clearly_instead_of_hanging_and_still_cleans_up(mosquitto_broker, mosquitto_host):
    @contextlib.contextmanager
    def _outage_that_never_clears(publisher):
        publisher.inject_connection_outage()
        yield  # deliberately never calls clear_connection_outage()

    logger_state_before = _gateway_logger_state()
    lines = []
    started = time.monotonic()

    result = run_scenario(DemoConfig(
        mqtt_host=mosquitto_host, mqtt_port=mosquitto_broker, announce=lines.append,
        outage=_outage_that_never_clears, recovery_timeout_seconds=2.0, **SMALL,
    ))

    assert time.monotonic() - started < 30
    assert result.passed is False
    assert result.failed_stage == "recovery"
    assert "timed out" in result.failure_reason and "reconnect" in result.failure_reason
    assert result.recovered is False
    assert result.metrics["buffered"] >= SMALL["outage_events"]  # it did get as far as buffering
    assert result.cleanup_completed is True and result.threads_alive_after_cleanup == 0
    assert _gateway_logger_state() == logger_state_before
    assert not _announced_buffer_dir(lines).exists()


def test_an_unexpected_error_mid_scenario_is_a_clean_fail_with_cleanup(mosquitto_broker, mosquitto_host):
    @contextlib.contextmanager
    def _broken_outage(publisher):
        raise RuntimeError("fault injection exploded")
        yield  # pragma: no cover

    result = run_scenario(DemoConfig(
        mqtt_host=mosquitto_host, mqtt_port=mosquitto_broker, announce=lambda _: None,
        outage=_broken_outage, **SMALL,
    ))

    assert result.passed is False
    assert result.failed_stage == "normal operation"  # the last stage that had started
    assert "RuntimeError: fault injection exploded" in result.failure_reason
    assert result.cleanup_completed is True and result.threads_alive_after_cleanup == 0
