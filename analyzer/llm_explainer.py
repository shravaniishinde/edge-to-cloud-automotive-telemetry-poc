"""
Optional LLM-based advisory explanation layer (Phase 6).

RULES DETECT. LLM EXPLAINS. This is the only file in `analyzer/` that
imports the `anthropic` SDK or knows an API key exists, and it does
exactly one thing: turn an already-decided `AnomalyReport` (plus the
`DiagnosticEvent`s behind it) into a short, human-readable explanation
string. It cannot do anything else -- there is no code path here that
creates an anomaly, changes a severity, sends a UDS request, or touches
telemetry. `analyzer/analyzer.py` never imports this module; a caller
must explicitly opt in after `DiagnosticAnalyzer.analyze()` has already
produced its findings, so this layer is structurally incapable of
suppressing or altering what the deterministic rules found. See
docs/analyzer-spec.md for the full "LLM is advisory only" discussion.

Fully optional, on purpose:
- No `ANTHROPIC_API_KEY` set (the default -- including in CI, and for
  anyone who hasn't set one up) -> `explain_anomaly()` returns `None`
  immediately, without importing `anthropic` or attempting any network
  call. The rest of the analyzer is completely unaffected.
- The API key is read from the `ANTHROPIC_API_KEY` environment variable
  only -- never hardcoded, never accepted as a function argument, never
  logged.
- A network error, timeout, or any other API failure is caught and also
  produces `None` (with a warning logged) rather than raising -- a flaky
  LLM call must never be able to crash or interrupt analysis.
"""

from __future__ import annotations

import logging
import os
from typing import List, Mapping, Optional

from common.diagnostic_schema import DiagnosticEvent
from analyzer.models import AnomalyReport

ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_MODEL = "ANTHROPIC_MODEL"

# A small, fast, inexpensive model is all a two-sentence advisory summary
# needs. Overridable via ANTHROPIC_MODEL, since model IDs are retired and
# replaced over time -- check https://docs.claude.com for the current
# list if this default ever stops working.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

MAX_RESPONSE_TOKENS = 200

_SYSTEM_PROMPT = (
    "You write short, plain-language explanations of automotive diagnostic "
    "findings for an engineering portfolio project. Rules: (1) You are "
    "explaining a finding that a deterministic rule has ALREADY made -- you "
    "never decide whether something is anomalous, and you never invent a "
    "different severity or conclusion than the one given to you. (2) Keep "
    "the explanation to 2-3 sentences. (3) Always make clear this is "
    "advisory, not a safety-critical or certified determination -- this is "
    "a proof-of-concept, not production automotive software. (4) Never "
    "suggest a specific repair action or safety verdict (e.g. do not say "
    "'stop driving' or 'this part has failed')."
)

logger = logging.getLogger(__name__)


def is_llm_configured(env: Optional[Mapping[str, str]] = None) -> bool:
    """True only if an API key is actually present. Used both internally
    and by callers that want to know up front whether asking for an
    explanation is even worth attempting (e.g. to skip building a prompt
    for a whole batch of reports)."""
    env = os.environ if env is None else env
    return bool(env.get(ENV_API_KEY))


def _build_prompt(report: AnomalyReport, events: List[DiagnosticEvent]) -> str:
    event_lines = "\n".join(
        f"- service={event.service_name} positive={event.is_positive_response} "
        f"negative_response_code={event.negative_response_code} "
        f"response_summary={event.response_summary!r} timestamp={event.timestamp.isoformat()}"
        for event in events
    )
    return (
        f"Rule finding:\n"
        f"  rule_id: {report.rule_id}\n"
        f"  severity: {report.severity.value}\n"
        f"  title: {report.title}\n"
        f"  description: {report.description}\n"
        f"  source_ecu: {report.source_ecu.value}\n"
        f"  vehicle_id: {report.vehicle_id}\n\n"
        f"Underlying diagnostic events ({len(events)}):\n{event_lines}\n\n"
        "Write the 2-3 sentence advisory explanation now."
    )


def explain_anomaly(
    report: AnomalyReport,
    events: List[DiagnosticEvent],
    *,
    env: Optional[Mapping[str, str]] = None,
    client: Optional[object] = None,
) -> Optional[str]:
    """Returns a short advisory explanation string, or `None` if the LLM
    isn't configured or the call fails for any reason. Never raises.

    `client` is an injection point for tests (pass a fake object with a
    `.messages.create(...)` method matching `anthropic.Anthropic`'s shape)
    so this can be exercised without a real API key or network call; real
    callers should leave it as `None` and let this function build the
    real `anthropic.Anthropic()` client itself.
    """
    env = os.environ if env is None else env
    if not is_llm_configured(env):
        return None

    if client is None:
        try:
            import anthropic
        except ImportError:
            logger.warning("ANTHROPIC_API_KEY is set but the anthropic package is not installed")
            return None
        client = anthropic.Anthropic(api_key=env[ENV_API_KEY])

    model = env.get(ENV_MODEL, DEFAULT_MODEL)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_RESPONSE_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_prompt(report, events)}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        return text or None
    except Exception:  # noqa: BLE001 - a flaky LLM call must never crash analysis
        logger.warning("LLM explanation request failed", exc_info=True)
        return None
