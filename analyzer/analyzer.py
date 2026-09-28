"""
DiagnosticAnalyzer (Phase 6): wires the deterministic rules in
`analyzer/rules.py` together into one small, pure entry point.

    UDS Server -> DiagnosticEvent -> DiagnosticAnalyzer.analyze()
        -> deterministic rules -> AnomalyReport(s) -> (optional) LLM explanation

`DiagnosticAnalyzer` holds no state between calls -- it does not
remember which events it has already seen, does not deduplicate against a
previous run, and does not read a clock. `analyze(events)` is a pure
function of its `events` argument: the same list in always produces the
same list of `AnomalyReport`s out, whether called once or a hundred times.
That statelessness is deliberate (see the class docstring below and
docs/analyzer-spec.md) -- it's what makes "run the analyzer over this
session's diagnostic log" a safe, repeatable operation rather than
something whose result depends on when or how many times it's called.

This module never imports `analyzer.llm_explainer` -- attaching an LLM
explanation to a report (if one is wanted at all) is a separate, optional
step a caller opts into after `analyze()` has already produced its
(unaffected) findings. See `analyzer/llm_explainer.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from common.diagnostic_schema import DiagnosticEvent
from analyzer.models import AnomalyReport
from analyzer.rules import (
    DEFAULT_DTC_QUERY_THRESHOLD,
    DEFAULT_DTC_QUERY_WINDOW_SECONDS,
    DEFAULT_NEGATIVE_RESPONSE_THRESHOLD,
    DEFAULT_NEGATIVE_RESPONSE_WINDOW_SECONDS,
    DEFAULT_P0217_THRESHOLD,
    DEFAULT_P0217_WINDOW_SECONDS,
    detect_repeated_dtc_queries,
    detect_repeated_negative_responses,
    detect_repeated_p0217_activity,
)


@dataclass(frozen=True)
class AnalyzerConfig:
    """Every rule's threshold/time-window, all in one place with the same
    defaults `analyzer/rules.py` itself uses. Override only what you need,
    e.g. `AnalyzerConfig(negative_response_threshold=5)` -- this is a
    frozen dataclass (not a dict) so a typo'd field name fails immediately
    instead of silently being ignored."""

    negative_response_threshold: int = DEFAULT_NEGATIVE_RESPONSE_THRESHOLD
    negative_response_window_seconds: float = DEFAULT_NEGATIVE_RESPONSE_WINDOW_SECONDS
    dtc_query_threshold: int = DEFAULT_DTC_QUERY_THRESHOLD
    dtc_query_window_seconds: float = DEFAULT_DTC_QUERY_WINDOW_SECONDS
    p0217_threshold: int = DEFAULT_P0217_THRESHOLD
    p0217_window_seconds: float = DEFAULT_P0217_WINDOW_SECONDS


class DiagnosticAnalyzer:
    """Stateless by design: construct one, call `analyze()` as many times
    as you like with whatever event list you have (a whole session's log,
    a live-growing buffer snapshot, a single test's worth of events) --
    nothing about a previous call affects the next one."""

    def __init__(self, config: AnalyzerConfig = AnalyzerConfig()) -> None:
        self._config = config

    def analyze(self, events: Sequence[DiagnosticEvent]) -> List[AnomalyReport]:
        """Runs every deterministic rule over `events` and returns all
        `AnomalyReport`s found, in a fixed rule order (see
        `analyzer/rules.ALL_RULES`). Returns an empty list for empty input
        or for events that trigger nothing -- never raises on "nothing
        found," since finding nothing is a perfectly normal result, not an
        error."""
        if not events:
            return []

        config = self._config
        reports: List[AnomalyReport] = []
        reports.extend(
            detect_repeated_negative_responses(
                events,
                threshold=config.negative_response_threshold,
                window_seconds=config.negative_response_window_seconds,
            )
        )
        reports.extend(
            detect_repeated_dtc_queries(
                events,
                threshold=config.dtc_query_threshold,
                window_seconds=config.dtc_query_window_seconds,
            )
        )
        reports.extend(
            detect_repeated_p0217_activity(
                events,
                threshold=config.p0217_threshold,
                window_seconds=config.p0217_window_seconds,
            )
        )
        return reports
