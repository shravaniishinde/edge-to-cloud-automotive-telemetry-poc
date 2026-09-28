"""Tests for DiagnosticAnalyzer -- the orchestration layer over
analyzer/rules.py. These deliberately don't re-test each rule's window/
threshold edge cases (that's test_rules.py's job); they test that
DiagnosticAnalyzer wires the rules together correctly: all three run,
results combine, config overrides reach the right rule, and the whole
thing behaves as a pure, stateless function of its input."""

from datetime import datetime, timedelta, timezone

from common.diagnostic_schema import DiagnosticEvent
from common.telemetry_schema import DEFAULT_VEHICLE_ID, ECUSource
from analyzer.analyzer import AnalyzerConfig, DiagnosticAnalyzer

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


def test_empty_input_returns_empty_list():
    analyzer = DiagnosticAnalyzer()
    assert analyzer.analyze([]) == []


def test_normal_sequence_produces_no_anomalies():
    events = [_make_event(0), _make_event(5), _make_event(10)]
    analyzer = DiagnosticAnalyzer()
    assert analyzer.analyze(events) == []


def test_unrelated_single_events_do_not_trigger_anything():
    events = [
        _make_event(0, service_id=0x10, service_name="DiagnosticSessionControl",
                    request_summary="requested_session=defaultSession",
                    response_summary="session_active=defaultSession"),
        _make_event(60, service_id=0x19, service_name="ReadDTCInformation",
                    request_summary="subfunction=reportDTCByStatusMask",
                    response_summary="returned 2 static DTC(s): ['P0217', 'P0420']"),
    ]
    analyzer = DiagnosticAnalyzer()
    assert analyzer.analyze(events) == []


def test_analyzer_detects_repeated_negative_responses():
    events = [
        _make_event(0, is_positive_response=False, negative_response_code="RequestOutOfRange",
                    response_summary="requestOutOfRange (unsupported DID)"),
        _make_event(10, is_positive_response=False, negative_response_code="RequestOutOfRange",
                    response_summary="requestOutOfRange (unsupported DID)"),
        _make_event(20, is_positive_response=False, negative_response_code="RequestOutOfRange",
                    response_summary="requestOutOfRange (unsupported DID)"),
    ]
    analyzer = DiagnosticAnalyzer()
    reports = analyzer.analyze(events)
    assert len(reports) == 1
    assert reports[0].rule_id == "repeated_negative_responses"


def test_analyzer_can_detect_multiple_independent_anomalies_in_one_batch():
    # A burst of negative responses AND a burst of P0217 DTC queries, on
    # two different services, within the same event list.
    negative_events = [
        _make_event(i * 10, is_positive_response=False, negative_response_code="RequestOutOfRange",
                    response_summary="requestOutOfRange (unsupported DID)")
        for i in range(3)
    ]
    dtc_events = [
        _make_event(500 + i * 10, service_id=0x19, service_name="ReadDTCInformation",
                    request_summary="subfunction=reportDTCByStatusMask",
                    response_summary="returned 2 static DTC(s): ['P0217', 'P0420']")
        for i in range(3)
    ]
    analyzer = DiagnosticAnalyzer()
    reports = analyzer.analyze(negative_events + dtc_events)

    rule_ids = {report.rule_id for report in reports}
    # repeated_negative_responses, repeated_dtc_queries, AND
    # repeated_p0217_activity should all independently fire here.
    assert rule_ids == {"repeated_negative_responses", "repeated_dtc_queries", "repeated_p0217_activity"}


def test_config_overrides_change_which_rules_fire():
    events = [
        _make_event(0, is_positive_response=False, negative_response_code="RequestOutOfRange",
                    response_summary="requestOutOfRange (unsupported DID)"),
        _make_event(10, is_positive_response=False, negative_response_code="RequestOutOfRange",
                    response_summary="requestOutOfRange (unsupported DID)"),
    ]
    # Only 2 negative events -- default threshold (3) does not fire...
    default_analyzer = DiagnosticAnalyzer()
    assert default_analyzer.analyze(events) == []

    # ...but a config with threshold=2 does.
    lenient_analyzer = DiagnosticAnalyzer(AnalyzerConfig(negative_response_threshold=2))
    reports = lenient_analyzer.analyze(events)
    assert len(reports) == 1
    assert reports[0].rule_id == "repeated_negative_responses"


def test_repeated_analysis_of_the_same_events_is_consistent():
    events = [
        _make_event(i * 10, is_positive_response=False, negative_response_code="RequestOutOfRange",
                    response_summary="requestOutOfRange (unsupported DID)")
        for i in range(3)
    ]
    analyzer = DiagnosticAnalyzer()

    first_run = analyzer.analyze(events)
    second_run = analyzer.analyze(events)
    third_run = analyzer.analyze(list(reversed(events)))  # input order must not matter either

    assert len(first_run) == len(second_run) == len(third_run) == 1
    first_dump = first_run[0].model_dump(exclude={"anomaly_id"})
    second_dump = second_run[0].model_dump(exclude={"anomaly_id"})
    third_dump = third_run[0].model_dump(exclude={"anomaly_id"})
    assert first_dump == second_dump == third_dump


def test_analyzing_the_same_list_twice_does_not_accumulate_state():
    # A stateful analyzer that "remembers" what it already reported might
    # suppress the second call's findings. This analyzer must not do that.
    events = [
        _make_event(i * 10, is_positive_response=False, negative_response_code="RequestOutOfRange",
                    response_summary="requestOutOfRange (unsupported DID)")
        for i in range(3)
    ]
    analyzer = DiagnosticAnalyzer()
    analyzer.analyze(events)  # first call, deliberately discarded
    second_call_reports = analyzer.analyze(events)
    assert len(second_call_reports) == 1
