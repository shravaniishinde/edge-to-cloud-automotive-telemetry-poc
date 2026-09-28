"""Unit tests for the deterministic rules in analyzer/rules.py. Every test
builds its own small, explicit list of DiagnosticEvents with hand-picked
timestamps -- no real UDS server, no real clock -- so window/threshold
behavior is exercised precisely and deterministically."""

from datetime import datetime, timedelta, timezone

from common.diagnostic_schema import DiagnosticEvent
from common.telemetry_schema import DEFAULT_VEHICLE_ID, ECUSource
from analyzer.rules import (
    detect_repeated_dtc_queries,
    detect_repeated_negative_responses,
    detect_repeated_p0217_activity,
)

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_event(offset_seconds: float, **overrides) -> DiagnosticEvent:
    kwargs = dict(
        session_id="test-session",
        vehicle_id=DEFAULT_VEHICLE_ID,
        source_ecu=ECUSource.POWERTRAIN,
        service_id=0x22,
        service_name="ReadDataByIdentifier",
        request_summary="DID=0xF190",
        response_summary="VIN=SIMVIN00000000001",
        is_positive_response=True,
        timestamp=_BASE_TIME + timedelta(seconds=offset_seconds),
    )
    kwargs.update(overrides)
    return DiagnosticEvent(**kwargs)


def _negative_event(offset_seconds: float, **overrides) -> DiagnosticEvent:
    kwargs = dict(
        is_positive_response=False,
        negative_response_code="RequestOutOfRange",
        response_summary="requestOutOfRange (unsupported DID)",
    )
    kwargs.update(overrides)
    return _make_event(offset_seconds, **kwargs)


def _dtc_event(offset_seconds: float, *, dtcs=("P0217", "P0420"), **overrides) -> DiagnosticEvent:
    kwargs = dict(
        service_id=0x19,
        service_name="ReadDTCInformation",
        request_summary="subfunction=reportDTCByStatusMask",
        response_summary=f"returned {len(dtcs)} static DTC(s): {list(dtcs)}",
        is_positive_response=True,
    )
    kwargs.update(overrides)
    return _make_event(offset_seconds, **kwargs)


# --- detect_repeated_negative_responses ---


def test_no_anomaly_for_a_normal_diagnostic_sequence():
    events = [_make_event(0), _make_event(1), _dtc_event(2)]
    assert detect_repeated_negative_responses(events, threshold=3, window_seconds=60) == []


def test_repeated_negative_responses_within_window_triggers():
    events = [_negative_event(0), _negative_event(10), _negative_event(20)]
    reports = detect_repeated_negative_responses(events, threshold=3, window_seconds=60)
    assert len(reports) == 1
    report = reports[0]
    assert report.rule_id == "repeated_negative_responses"
    assert report.source_ecu == ECUSource.POWERTRAIN
    assert report.event_ids == [e.event_id for e in events]
    assert report.detected_at == events[-1].timestamp


def test_negative_responses_outside_window_do_not_trigger():
    # Same 3 events as the passing test above, but spread far enough apart
    # that no 3-event run falls within a 60s window.
    events = [_negative_event(0), _negative_event(100), _negative_event(200)]
    assert detect_repeated_negative_responses(events, threshold=3, window_seconds=60) == []


def test_negative_responses_below_threshold_do_not_trigger():
    events = [_negative_event(0), _negative_event(10)]
    assert detect_repeated_negative_responses(events, threshold=3, window_seconds=60) == []


def test_different_ecus_are_not_combined_for_negative_responses():
    events = [
        _negative_event(0, source_ecu=ECUSource.POWERTRAIN),
        _negative_event(10, source_ecu=ECUSource.BATTERY),
        _negative_event(20, source_ecu=ECUSource.POWERTRAIN),
    ]
    # Only 2 negative events per ECU -- below the threshold of 3 each,
    # even though 3 negative events exist in total across the whole list.
    assert detect_repeated_negative_responses(events, threshold=3, window_seconds=60) == []


def test_positive_responses_never_count_toward_negative_rule():
    events = [_make_event(0), _make_event(10), _make_event(20), _make_event(30)]
    assert detect_repeated_negative_responses(events, threshold=3, window_seconds=60) == []


# --- detect_repeated_dtc_queries ---


def test_repeated_dtc_queries_within_window_triggers():
    events = [_dtc_event(0), _dtc_event(15), _dtc_event(30)]
    reports = detect_repeated_dtc_queries(events, threshold=3, window_seconds=60)
    assert len(reports) == 1
    assert reports[0].rule_id == "repeated_dtc_queries"
    assert reports[0].event_ids == [e.event_id for e in events]


def test_dtc_queries_outside_window_do_not_trigger():
    events = [_dtc_event(0), _dtc_event(90), _dtc_event(200)]
    assert detect_repeated_dtc_queries(events, threshold=3, window_seconds=60) == []


def test_unrelated_service_events_do_not_count_as_dtc_queries():
    events = [_make_event(0), _make_event(10), _make_event(20)]  # ReadDataByIdentifier, not DTC
    assert detect_repeated_dtc_queries(events, threshold=3, window_seconds=60) == []


def test_different_ecus_are_not_combined_for_dtc_queries():
    events = [
        _dtc_event(0, source_ecu=ECUSource.POWERTRAIN),
        _dtc_event(10, source_ecu=ECUSource.POWERTRAIN),
        _dtc_event(20, source_ecu=ECUSource.BATTERY),
        _dtc_event(30, source_ecu=ECUSource.BATTERY),
    ]
    # 2 per ECU, below a threshold of 3.
    assert detect_repeated_dtc_queries(events, threshold=3, window_seconds=60) == []


# --- detect_repeated_p0217_activity ---


def test_repeated_p0217_activity_triggers():
    events = [_dtc_event(0), _dtc_event(20)]
    reports = detect_repeated_p0217_activity(events, threshold=2, window_seconds=60)
    assert len(reports) == 1
    assert reports[0].rule_id == "repeated_p0217_activity"
    assert "P0217" in reports[0].title


def test_p0217_activity_outside_window_does_not_trigger():
    events = [_dtc_event(0), _dtc_event(200)]
    assert detect_repeated_p0217_activity(events, threshold=2, window_seconds=60) == []


def test_dtc_queries_without_p0217_do_not_trigger_p0217_rule():
    events = [_dtc_event(0, dtcs=("P0420",)), _dtc_event(20, dtcs=("P0420",))]
    assert detect_repeated_p0217_activity(events, threshold=2, window_seconds=60) == []


def test_negative_dtc_responses_do_not_trigger_p0217_rule():
    # A negative response can't mention a DTC label at all in this
    # project's server implementation, but the rule should not assume
    # that -- it explicitly requires is_positive_response=True.
    events = [
        _dtc_event(0, is_positive_response=False, negative_response_code="SubFunctionNotSupported",
                   response_summary="subFunctionNotSupported"),
        _dtc_event(20, is_positive_response=False, negative_response_code="SubFunctionNotSupported",
                   response_summary="subFunctionNotSupported"),
    ]
    assert detect_repeated_p0217_activity(events, threshold=2, window_seconds=60) == []


def test_different_ecus_are_not_combined_for_p0217_rule():
    events = [
        _dtc_event(0, source_ecu=ECUSource.POWERTRAIN),
        _dtc_event(20, source_ecu=ECUSource.BATTERY),
    ]
    assert detect_repeated_p0217_activity(events, threshold=2, window_seconds=60) == []


# --- shared clustering behavior ---


def test_empty_input_is_handled_safely_for_every_rule():
    assert detect_repeated_negative_responses([]) == []
    assert detect_repeated_dtc_queries([]) == []
    assert detect_repeated_p0217_activity([]) == []


def test_one_burst_produces_one_report_not_one_per_event():
    # 5 negative events, all within the window -- a naive "check every
    # event's trailing window" implementation would emit 3 overlapping
    # reports here (once threshold is first met, and again for each event
    # after it); clustering should instead emit exactly one.
    events = [_negative_event(i * 5) for i in range(5)]
    reports = detect_repeated_negative_responses(events, threshold=3, window_seconds=60)
    assert len(reports) == 1
    assert len(reports[0].event_ids) == 5


def test_two_separate_bursts_produce_two_reports():
    first_burst = [_negative_event(0), _negative_event(10), _negative_event(20)]
    second_burst = [_negative_event(200), _negative_event(210), _negative_event(220)]
    events = first_burst + second_burst
    reports = detect_repeated_negative_responses(events, threshold=3, window_seconds=60)
    assert len(reports) == 2
    assert reports[0].event_ids == [e.event_id for e in first_burst]
    assert reports[1].event_ids == [e.event_id for e in second_burst]


def test_repeated_calls_on_the_same_input_are_identical():
    events = [_negative_event(0), _negative_event(10), _negative_event(20)]
    first_run = detect_repeated_negative_responses(events, threshold=3, window_seconds=60)
    second_run = detect_repeated_negative_responses(events, threshold=3, window_seconds=60)
    assert len(first_run) == len(second_run) == 1
    # Every field except the randomly-generated anomaly_id (same convention
    # as TelemetryEvent.event_id/DiagnosticEvent.event_id: an identity, not
    # a computed result) must match exactly between the two runs.
    first_dump = first_run[0].model_dump(exclude={"anomaly_id"})
    second_dump = second_run[0].model_dump(exclude={"anomaly_id"})
    assert first_dump == second_dump
