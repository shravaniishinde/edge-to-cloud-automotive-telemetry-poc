# Diagnostic Anomaly Analyzer Specification (Phase 6)

The human-readable mirror of `analyzer/`, the same relationship
`docs/uds-spec.md` has to `simulation/uds/`. If this file and the code
ever disagree, the code is correct and this file is stale.

## Core principle: RULES DETECT, LLM EXPLAINS

This is the one rule the whole analyzer is built around. Deterministic,
hand-written rules (`analyzer/rules.py`) are the *only* thing that decides
whether something is an anomaly, what its severity is, and which events
caused it. An optional LLM layer (`analyzer/llm_explainer.py`) can
afterward turn an already-decided finding into a short paragraph of plain
English -- and that is *all* it can do. It cannot create a finding, delete
one, change a severity, or talk the analyzer out of something the rules
already found. `analyzer/analyzer.py` (the orchestrator) never even
imports `llm_explainer.py` -- attaching an explanation is a separate,
optional step a caller takes after `DiagnosticAnalyzer.analyze()` has
already returned its (unaffected) findings.

The analyzer works completely without an LLM API key. This is not a
best-effort fallback -- it is the normal, fully-supported mode, and it is
how CI and anyone without an Anthropic API key runs it.

## Where this fits in the data flow

```
UDS Server (simulation/uds/uds_server.py)
    |
DiagnosticEvent (common/diagnostic_schema.py) -- one per UDS transaction
    |
DiagnosticAnalyzer.analyze(events)  [analyzer/analyzer.py]
    |
    +--> detect_repeated_negative_responses()   \
    +--> detect_repeated_dtc_queries()            } analyzer/rules.py
    +--> detect_repeated_p0217_activity()        /
    |
AnomalyReport(s)  [analyzer/models.py]
    |
(optional, caller-initiated) explain_anomaly(report, events)  [analyzer/llm_explainer.py]
    |
AnomalyReport.llm_explanation populated, everything else unchanged
```

Note what is *not* in this diagram: there is no queue, no database, and
no background worker. `DiagnosticAnalyzer` is a plain Python object you
call `.analyze(events)` on with whatever `DiagnosticEvent` list you
already have -- a whole session's worth collected via the `on_event`
callback `simulation/uds/uds_server.run_server()` already exposes (see
`simulation/uds/tests/test_uds_integration.py` for the exact pattern:
`events_received.append` passed as `on_event`), a single test's events,
or, in a later phase, whatever a dashboard backend is holding in memory.
Phase 6 deliberately does not add a persistence layer for diagnostic
events or anomaly reports -- see "Limitations" below.

## Statelessness, and why it matters here specifically

`DiagnosticAnalyzer.analyze()` is a pure function: `(events) -> reports`,
nothing else. It does not remember a previous call, does not deduplicate
against reports it already returned, and does not read a clock --
`detected_at` is the timestamp of the last event in the triggering
cluster, not `datetime.now()`. Two consequences fall directly out of this:

- Calling `analyze()` twice on the *same* event list returns the *same*
  reports both times (aside from each `AnomalyReport.anomaly_id`, which is
  a freshly-generated identity on every call -- the same convention
  `TelemetryEvent.event_id`/`DiagnosticEvent.event_id` already use, not a
  computed result). See `analyzer/tests/test_analyzer.py`'s
  `test_repeated_analysis_of_the_same_events_is_consistent`.
- Nothing about *when* you happen to call `analyze()`, or how many times
  you've already called it, changes what it finds. A dashboard (a later
  phase) could safely re-run analysis over the same session log on every
  page load without the results drifting.

## The three deterministic rules

All three rules group events by `source_ecu` first and never combine
activity from two different ECUs into one finding -- a burst split across
two ECUs is two separate, smaller stories, not one. All three use the
same single-pass time-window clustering helper
(`analyzer/rules._cluster_by_window`): a maximal run of at least
`threshold` matching events that all fall within `window_seconds` of the
run's first event becomes exactly one `AnomalyReport`, not one per event
in it.

| Rule ID | What it looks at | Default threshold | Default window |
|---|---|---|---|
| `repeated_negative_responses` | Negative `DiagnosticEvent`s (`is_positive_response=False`), any service | 3 | 60s |
| `repeated_dtc_queries` | `ReadDTCInformation` (0x19) requests, any outcome | 3 | 60s |
| `repeated_p0217_activity` | Positive `ReadDTCInformation` responses whose `response_summary` mentions `P0217` | 2 | 60s |

`repeated_p0217_activity` uses a lower threshold than
`repeated_dtc_queries` on purpose: "someone is polling DTCs" (Rule 2,
unconcerned with which DTC comes back) is a much broader, more often
benign pattern than "a specific overtemperature-related code keeps
appearing" (Rule 3) -- the narrower rule is allowed to fire a little
sooner. This is a POC judgment call, not a tuned or validated automotive
engineering threshold.

## Configurable time windows

Every threshold/window pair above is a constructor argument, not a
hardcoded constant: pass an `analyzer.AnalyzerConfig` to
`DiagnosticAnalyzer` to override any subset of them, e.g.
`DiagnosticAnalyzer(AnalyzerConfig(negative_response_threshold=5))`. Each
individual rule function in `analyzer/rules.py` also accepts
`threshold`/`window_seconds` directly, for tests or standalone use.

## Severity levels

Three plain levels (`analyzer.Severity`: `info`, `warning`, `critical`),
not a numeric score -- deliberately coarse, since this is a POC advisory
tool, not a certified diagnostic system. Current assignments:
`repeated_negative_responses` -> `warning`, `repeated_dtc_queries` ->
`info`, `repeated_p0217_activity` -> `warning` (not `critical` --
see "Avoiding exaggerated claims" below).

## Avoiding exaggerated claims

None of these rules are safety-critical vehicle diagnostics, and none of
their output should be described that way. Specifically:

- `repeated_p0217_activity` is deliberately rated `warning`, not
  `critical`, even though the underlying DTC concerns engine
  overtemperature. Escalating a POC rule's output to the analyzer's
  highest severity level for what is, underneath, a repeated read of a
  static, illustrative DTC list (see `common/diagnostic_schema.py`) would
  overstate what this system actually knows.
- Every rule's `description` text explicitly says "advisory" and "not a
  safety-critical determination" (or equivalent), and the LLM system
  prompt (`analyzer/llm_explainer.py`) is instructed never to suggest a
  repair action or a safety verdict.
- Neither a rule nor the optional LLM layer ever claims certainty about
  root cause -- only that a pattern was observed.

## False positives and false negatives (by design, not by accident)

These are threshold-based POC rules, not a trained or validated
detection system, so both kinds of error are expected and worth naming
plainly:

- **False positives are likely for `repeated_dtc_queries`.** A technician
  or scan tool legitimately polling DTCs in a normal diagnostic session
  can easily exceed the default threshold. This rule is intentionally
  rated `info`, not `warning`, to reflect that its output is a data point,
  not evidence of a fault.
- **False negatives are possible for all three rules** if real activity
  is spread out more slowly than the configured window (e.g. negative
  responses 90 seconds apart, when the window is 60s) -- see
  `test_negative_responses_outside_window_do_not_trigger` and its
  siblings in `analyzer/tests/test_rules.py`. Widening a window trades
  fewer false negatives for a slower reaction and a higher chance of
  combining genuinely unrelated events into one finding.
- **`repeated_p0217_activity` will fire on essentially every DTC-query
  burst** in this POC, because `simulation/uds/uds_server.py`'s static DTC
  list always includes P0217 on every positive `ReadDTCInformation`
  response (the server does not filter by the client's requested status
  mask -- see `docs/uds-spec.md`). In a system where DTCs vary
  realistically, `repeated_dtc_queries` and `repeated_p0217_activity`
  would diverge much more than they can here; that's a property of this
  POC's simulated ECU, not a flaw in the rule itself.

None of this is tuned against real vehicle data, because none exists here
-- see `docs/assumptions-and-limitations.md`.

## The optional LLM explanation layer

`analyzer/llm_explainer.explain_anomaly(report, events)`:

- Reads `ANTHROPIC_API_KEY` from the environment only -- never hardcoded,
  never accepted as a function argument, never logged. No key set
  (default, including CI) -> returns `None` immediately, without
  importing `anthropic` or attempting any network call.
- The model is `claude-haiku-4-5-20251001` by default (a small, fast,
  inexpensive model, appropriate for a two-to-three sentence summary),
  overridable via the `ANTHROPIC_MODEL` environment variable. Model IDs
  are retired and replaced over time -- check
  [docs.claude.com](https://docs.claude.com) for the current list if this
  default ever stops working.
- Any failure (network error, timeout, missing package, malformed
  response) is caught and also produces `None` -- a flaky LLM call must
  never be able to crash or interrupt analysis. This is checked directly
  in `analyzer/tests/test_llm_explainer.py` by injecting a client whose
  `.messages.create()` raises.
- Accepts a `client=` argument purely as a test seam (inject a fake object
  matching `anthropic.Anthropic`'s `.messages.create(...)` shape); real
  callers leave it as `None` and let the function build the real client.
- Writing the returned string onto `AnomalyReport.llm_explanation` is the
  *caller's* choice, not something `explain_anomaly()` does itself -- the
  function returns a plain string (or `None`) and never mutates the
  report it was given (see
  `test_explain_anomaly_never_changes_the_report_itself`).

### What the LLM is not allowed to do

Enforced structurally, not just by convention:

- It cannot decide whether something is an anomaly -- `analyze()` has
  already run, fully, before `explain_anomaly()` is ever called.
- It cannot change a report's severity, rule_id, event_ids, or any other
  rule-decided field -- it is only ever given a report to *read*, and its
  only output is a plain string a caller may or may not attach.
- It cannot send a UDS request, modify telemetry, or reach any other part
  of this system -- `analyzer/llm_explainer.py` has no import of, or
  reference to, `simulation/`, `edge_gateway/`, or any live bus/broker
  connection.
- It cannot suppress a finding -- there is no code path from
  `explain_anomaly()` back into `DiagnosticAnalyzer.analyze()`'s output.

### Running without an LLM API key

This is the default, not a degraded mode: simply don't set
`ANTHROPIC_API_KEY`. `DiagnosticAnalyzer.analyze()` and every deterministic
rule work exactly the same either way; only
`AnomalyReport.llm_explanation` stays `None`. CI never sets this variable,
for the same reason it never sets AWS IoT credentials (see
`docs/aws-setup.md`) -- no external service should be required for the
test suite to pass.

## Limitations

- No persistence layer exists for `DiagnosticEvent`s or `AnomalyReport`s
  in this phase -- both live only in whatever list a caller (a test, or
  eventually a dashboard backend) is holding in memory. This mirrors how
  Phase 3's telemetry pipeline looked before Phase 4 added a buffer, and
  is an intentional, minimal scope for this phase -- "do not introduce
  databases, queues, or microservices for Phase 6" per this phase's plan.
- Rules operate on whatever event list they're given, in one batch --
  there is no live/streaming mode yet where new events are analyzed as
  they arrive. A caller wanting near-real-time analysis today would need
  to re-run `analyze()` periodically over an accumulating list.
- Thresholds and windows are POC defaults chosen for interview
  explainability, not tuned against real vehicle telemetry or validated
  false-positive/false-negative rates (none of this project's data is
  real -- see `docs/assumptions-and-limitations.md`).
- The LLM explanation, when present, is exactly as reliable as the
  underlying model's output for a short summarization task -- it is
  labeled advisory for that reason, and this project never treats it as
  authoritative.
