"""Tests for the optional LLM explanation layer. None of these make a
real network call or require a real API key -- the "configured" path
injects a fake client (matching anthropic.Anthropic's `.messages.create`
shape) via the `client=` parameter, exactly as documented in
explain_anomaly()'s docstring."""

from datetime import datetime, timezone

from common.diagnostic_schema import DiagnosticEvent
from common.telemetry_schema import DEFAULT_VEHICLE_ID, ECUSource
from analyzer.llm_explainer import ENV_API_KEY, explain_anomaly, is_llm_configured
from analyzer.models import AnomalyReport, Severity


def _make_report(**overrides) -> AnomalyReport:
    kwargs = dict(
        rule_id="repeated_negative_responses",
        severity=Severity.WARNING,
        title="Repeated negative UDS responses from powertrain",
        description="3 negative diagnostic responses were observed.",
        event_ids=["e1", "e2", "e3"],
        vehicle_id=DEFAULT_VEHICLE_ID,
        source_ecu=ECUSource.POWERTRAIN,
        detected_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return AnomalyReport(**kwargs)


def _make_event(**overrides) -> DiagnosticEvent:
    kwargs = dict(
        session_id="test-session",
        vehicle_id=DEFAULT_VEHICLE_ID,
        source_ecu=ECUSource.POWERTRAIN,
        service_id=0x22,
        service_name="ReadDataByIdentifier",
        request_summary="DID=0xF190",
        response_summary="requestOutOfRange (unsupported DID)",
        is_positive_response=False,
        negative_response_code="RequestOutOfRange",
    )
    kwargs.update(overrides)
    return DiagnosticEvent(**kwargs)


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._response_text)


class _FakeClient:
    def __init__(self, response_text: str = "This is advisory only.") -> None:
        self.messages = _FakeMessages(response_text)


def test_is_llm_configured_false_without_api_key():
    assert is_llm_configured({}) is False


def test_is_llm_configured_true_with_api_key():
    assert is_llm_configured({ENV_API_KEY: "sk-test-key"}) is True


def test_explain_anomaly_returns_none_without_api_key():
    report = _make_report()
    events = [_make_event()]
    # No client is even provided -- if this tried to use one, it would
    # fail immediately, proving explain_anomaly() short-circuits first.
    result = explain_anomaly(report, events, env={})
    assert result is None


def test_explain_anomaly_returns_text_when_configured():
    report = _make_report()
    events = [_make_event()]
    fake_client = _FakeClient(response_text="Repeated negative responses were observed. Advisory only.")

    result = explain_anomaly(report, events, env={ENV_API_KEY: "sk-test-key"}, client=fake_client)

    assert result == "Repeated negative responses were observed. Advisory only."
    assert len(fake_client.messages.calls) == 1


def test_explain_anomaly_sends_the_rule_decided_severity_not_its_own():
    report = _make_report(severity=Severity.CRITICAL)
    events = [_make_event()]
    fake_client = _FakeClient()

    explain_anomaly(report, events, env={ENV_API_KEY: "sk-test-key"}, client=fake_client)

    sent_prompt = fake_client.messages.calls[0]["messages"][0]["content"]
    assert "critical" in sent_prompt


def test_explain_anomaly_returns_none_when_the_client_raises():
    class _RaisingMessages:
        def create(self, **kwargs):
            raise RuntimeError("simulated network failure")

    class _RaisingClient:
        def __init__(self) -> None:
            self.messages = _RaisingMessages()

    report = _make_report()
    events = [_make_event()]

    result = explain_anomaly(report, events, env={ENV_API_KEY: "sk-test-key"}, client=_RaisingClient())

    assert result is None


def test_explain_anomaly_never_changes_the_report_itself():
    report = _make_report()
    original_severity = report.severity
    original_title = report.title
    events = [_make_event()]

    explain_anomaly(report, events, env={ENV_API_KEY: "sk-test-key"}, client=_FakeClient())

    # explain_anomaly() returns a string; it is the CALLER's choice whether
    # to attach it to the report (e.g. report.llm_explanation = result).
    # The function itself must never mutate the report it was given.
    assert report.severity == original_severity
    assert report.title == original_title
    assert report.llm_explanation is None


def test_explain_anomaly_default_env_path_makes_no_call_without_a_key():
    """The default `env=None` path reads os.environ. The repo-root
    conftest.py strips ANTHROPIC_API_KEY for every test, so even with a
    real key in the developer's shell this must short-circuit before ever
    touching a client."""

    # explain_anomaly() swallows client exceptions by design, so a raising
    # tripwire could pass silently -- record calls instead.
    tripwire = _FakeClient()

    assert is_llm_configured() is False
    assert explain_anomaly(_make_report(), [_make_event()], client=tripwire) is None
    assert tripwire.messages.calls == []
