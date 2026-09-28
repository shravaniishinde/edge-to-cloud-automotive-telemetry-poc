"""
Deterministic anomaly rules (Phase 6).

RULES DETECT. LLM EXPLAINS. Every function in this file is a small, pure,
independently-testable function: `(events) -> List[AnomalyReport]`. None
of them touch a network, a clock (`datetime.now()`), randomness, or any
state outside their own arguments -- a rule's output depends only on the
`DiagnosticEvent`s it's given, so calling the same rule twice on the same
input always returns the same anomalies. That determinism is deliberate:
it's what makes these rules trustworthy enough to be the authority for
anomaly detection, and it's what an optional, occasionally-wrong LLM layer
must never be allowed to replace (see analyzer/llm_explainer.py).

These are POC anomaly rules, not safety-critical vehicle diagnostics --
see docs/analyzer-spec.md for the full discussion of false positives/
negatives and why each threshold is a "reasonable POC default," not a
tuned or validated automotive engineering value.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Sequence

from common.diagnostic_schema import DiagnosticEvent
from common.telemetry_schema import ECUSource
from analyzer.models import AnomalyReport, Severity

# --- Service/DTC identifiers rules key off of ---
# Matched by name (a plain string already on DiagnosticEvent), not by
# importing udsoncan's service classes here -- the analyzer package has no
# reason to depend on the UDS library itself, only on the shared
# DiagnosticEvent shape. See docs/uds-spec.md for the SID (0x19) this name
# corresponds to.
READ_DTC_SERVICE_NAME = "ReadDTCInformation"
P0217_LABEL = "P0217"  # engine overtemperature DTC, see common/diagnostic_schema.py

# --- Rule 1: repeated negative UDS responses ---
DEFAULT_NEGATIVE_RESPONSE_THRESHOLD = 3
DEFAULT_NEGATIVE_RESPONSE_WINDOW_SECONDS = 60.0

# --- Rule 2: repeated DTC queries ---
DEFAULT_DTC_QUERY_THRESHOLD = 3
DEFAULT_DTC_QUERY_WINDOW_SECONDS = 60.0

# --- Rule 3: repeated P0217 / overheating-related activity ---
# A lower threshold than the generic DTC-query rule above is deliberate:
# repeated *DTC queries* alone could just be a technician's tool polling
# (Rule 2 exists for that, generic and unconcerned with *which* DTC comes
# back), but a specific overtemperature-related code continuing to show up
# is a more specific pattern worth flagging a little sooner. This is a POC
# judgment call, not a tuned automotive threshold -- see
# docs/analyzer-spec.md.
DEFAULT_P0217_THRESHOLD = 2
DEFAULT_P0217_WINDOW_SECONDS = 60.0


def _group_by_ecu(events: Sequence[DiagnosticEvent]) -> Dict[ECUSource, List[DiagnosticEvent]]:
    """Splits events by source_ecu and sorts each group by timestamp.
    Every rule below groups by ECU first, on purpose: a burst of activity
    split across two different ECUs is two separate, smaller stories, not
    one combined anomaly -- see docs/analyzer-spec.md."""
    grouped: Dict[ECUSource, List[DiagnosticEvent]] = defaultdict(list)
    for event in events:
        grouped[event.source_ecu].append(event)
    for ecu_events in grouped.values():
        ecu_events.sort(key=lambda event: event.timestamp)
    return grouped


def _cluster_by_window(
    events: Sequence[DiagnosticEvent], threshold: int, window_seconds: float
) -> List[List[DiagnosticEvent]]:
    """Given events already sorted by timestamp (all for one ECU, all
    already filtered to the kind of event a rule cares about), finds
    maximal runs of at least `threshold` events that all fall within
    `window_seconds` of the run's *first* event.

    Single pass, deterministic, and non-overlapping: once a qualifying
    cluster is found, scanning resumes immediately after it, so one burst
    of activity produces exactly one anomaly, not one per event inside it
    (which is what a naive "check every event's trailing window"
    approach would do).
    """
    clusters: List[List[DiagnosticEvent]] = []
    n = len(events)
    start = 0
    while start <= n - threshold:
        end = start
        while (
            end + 1 < n
            and (events[end + 1].timestamp - events[start].timestamp).total_seconds() <= window_seconds
        ):
            end += 1
        cluster_size = end - start + 1
        if cluster_size >= threshold:
            clusters.append(list(events[start : end + 1]))
            start = end + 1
        else:
            start += 1
    return clusters


def _build_report(
    *, rule_id: str, severity: Severity, title: str, description: str, cluster: List[DiagnosticEvent]
) -> AnomalyReport:
    """Shared report-construction step for every rule below. `detected_at`
    is the timestamp of the *last* event in the cluster (not
    `datetime.now()`) -- deliberately, so a rule's output is a pure
    function of its input events and calling `analyze()` twice on the same
    events produces identical reports, not ones that differ by whatever
    moment each call happened to run at."""
    first_event = cluster[0]
    return AnomalyReport(
        rule_id=rule_id,
        severity=severity,
        title=title,
        description=description,
        event_ids=[event.event_id for event in cluster],
        vehicle_id=first_event.vehicle_id,
        source_ecu=first_event.source_ecu,
        detected_at=cluster[-1].timestamp,
    )


def detect_repeated_negative_responses(
    events: Sequence[DiagnosticEvent],
    *,
    threshold: int = DEFAULT_NEGATIVE_RESPONSE_THRESHOLD,
    window_seconds: float = DEFAULT_NEGATIVE_RESPONSE_WINDOW_SECONDS,
) -> List[AnomalyReport]:
    """Rule 1: flags a burst of >= `threshold` negative DiagnosticEvents
    from the SAME ECU within `window_seconds`. A negative response on its
    own is normal UDS behavior (e.g. an unsupported DID); a cluster of them
    is what's worth a human's attention -- possibly a diagnostic tool
    misconfigured against this ECU, or a genuine ECU communication
    problem. This rule does not attempt to tell those two apart -- see
    docs/analyzer-spec.md."""
    reports: List[AnomalyReport] = []
    for ecu_events in _group_by_ecu(events).values():
        negative_events = [event for event in ecu_events if not event.is_positive_response]
        for cluster in _cluster_by_window(negative_events, threshold, window_seconds):
            codes = sorted({event.negative_response_code or "unknown" for event in cluster})
            reports.append(
                _build_report(
                    rule_id="repeated_negative_responses",
                    severity=Severity.WARNING,
                    title=f"Repeated negative UDS responses from {cluster[0].source_ecu.value}",
                    description=(
                        f"{len(cluster)} negative diagnostic responses were observed from the "
                        f"{cluster[0].source_ecu.value} ECU within {window_seconds:.0f}s "
                        f"(negative response code(s): {', '.join(codes)}). This may indicate "
                        "repeated diagnostic access failures or an ECU communication issue; it "
                        "is an advisory finding, not a safety-critical determination."
                    ),
                    cluster=cluster,
                )
            )
    return reports


def detect_repeated_dtc_queries(
    events: Sequence[DiagnosticEvent],
    *,
    threshold: int = DEFAULT_DTC_QUERY_THRESHOLD,
    window_seconds: float = DEFAULT_DTC_QUERY_WINDOW_SECONDS,
) -> List[AnomalyReport]:
    """Rule 2: flags >= `threshold` ReadDTCInformation (0x19) requests from
    the SAME ECU within `window_seconds`, regardless of what the response
    contained. Repeated DTC polling is often completely benign (a
    technician's scan tool refreshing its display), but a sustained burst
    is still worth surfacing as a data point -- see
    docs/analyzer-spec.md."""
    reports: List[AnomalyReport] = []
    for ecu_events in _group_by_ecu(events).values():
        dtc_events = [event for event in ecu_events if event.service_name == READ_DTC_SERVICE_NAME]
        for cluster in _cluster_by_window(dtc_events, threshold, window_seconds):
            reports.append(
                _build_report(
                    rule_id="repeated_dtc_queries",
                    severity=Severity.INFO,
                    title=f"Repeated DTC queries against {cluster[0].source_ecu.value}",
                    description=(
                        f"{len(cluster)} ReadDTCInformation requests were observed against the "
                        f"{cluster[0].source_ecu.value} ECU within {window_seconds:.0f}s. This is "
                        "often benign (e.g. a scan tool polling), but is surfaced as a data point; "
                        "it is not, by itself, evidence of a fault."
                    ),
                    cluster=cluster,
                )
            )
    return reports


def detect_repeated_p0217_activity(
    events: Sequence[DiagnosticEvent],
    *,
    threshold: int = DEFAULT_P0217_THRESHOLD,
    window_seconds: float = DEFAULT_P0217_WINDOW_SECONDS,
) -> List[AnomalyReport]:
    """Rule 3: flags >= `threshold` DTC-query responses that reported the
    P0217 (engine overtemperature, illustrative -- see
    common/diagnostic_schema.py) code from the SAME ECU within
    `window_seconds`. Narrower than Rule 2 on purpose: this only counts
    responses that actually mention P0217, not every DTC query, so it can
    fire on a smaller, faster-triggering threshold without conflating
    "someone is polling DTCs" with "an overtemperature code keeps coming
    back." Still advisory only -- see docs/analyzer-spec.md for why this
    is not a safety determination even though the underlying DTC concerns
    engine temperature."""
    reports: List[AnomalyReport] = []
    for ecu_events in _group_by_ecu(events).values():
        p0217_events = [
            event
            for event in ecu_events
            if event.service_name == READ_DTC_SERVICE_NAME
            and event.is_positive_response
            and P0217_LABEL in event.response_summary
        ]
        for cluster in _cluster_by_window(p0217_events, threshold, window_seconds):
            reports.append(
                _build_report(
                    rule_id="repeated_p0217_activity",
                    severity=Severity.WARNING,
                    title=f"Repeated {P0217_LABEL} (overtemperature) activity on {cluster[0].source_ecu.value}",
                    description=(
                        f"The {P0217_LABEL} diagnostic trouble code (illustrative: engine "
                        f"overtemperature) was reported {len(cluster)} times by the "
                        f"{cluster[0].source_ecu.value} ECU within {window_seconds:.0f}s. This may "
                        "indicate a persistent thermal condition worth investigating; it is an "
                        "advisory finding from a POC rule set, not a certified diagnostic or "
                        "safety-critical determination."
                    ),
                    cluster=cluster,
                )
            )
    return reports


# Fixed order: every DiagnosticAnalyzer.analyze() call runs rules in this
# same sequence, so the order of reports in its output is itself
# deterministic, not just each rule's own output.
ALL_RULES = (
    detect_repeated_negative_responses,
    detect_repeated_dtc_queries,
    detect_repeated_p0217_activity,
)
