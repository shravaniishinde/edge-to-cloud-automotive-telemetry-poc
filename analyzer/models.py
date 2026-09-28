"""
Shared data model for the Diagnostic Anomaly Analyzer (Phase 6).

Mirrors the philosophy of `common/telemetry_schema.py` and
`common/diagnostic_schema.py`: one definition of "what an anomaly finding
looks like," reused by the deterministic rules, the analyzer that wires
them together, the optional LLM explainer, and their tests.

`AnomalyReport` is deliberately NOT added to `common/` alongside
TelemetryEvent/DiagnosticEvent: those two are inputs shared across many
components (simulator, gateway, cloud, analyzer). AnomalyReport is an
*output* that, for now, only the analyzer itself produces and consumes
(a later dashboard phase would read it, but nothing produces or expects
it before this phase exists) -- keeping it here keeps the analyzer
package self-contained without stretching `common/`'s job.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from common.telemetry_schema import ECUSource

SCHEMA_VERSION = "1.0"


class Severity(str, Enum):
    """
    Deliberately just three plain levels, not a numeric score -- this is a
    POC advisory tool, not a certified diagnostic system (see
    docs/analyzer-spec.md). Ordered loosely least-to-most attention-worthy,
    but a rule is free to pick whichever level fits it; nothing here
    implies a safety determination.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AnomalyReport(BaseModel):
    """
    One finding produced by a single deterministic rule, over a specific
    cluster of `DiagnosticEvent`s. Validates structure only, same
    philosophy as `TelemetryEvent`/`DiagnosticEvent`: this does not itself
    judge severity beyond what the rule that built it already decided.

    `llm_explanation` is the ONLY field the optional LLM layer
    (`analyzer/llm_explainer.py`) is ever allowed to fill in. Every other
    field is set once, by a deterministic rule, before the report exists --
    the LLM is never in a position to invent, delete, or re-classify a
    finding. See docs/analyzer-spec.md's "LLM is advisory only" section.
    """

    anomaly_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    rule_id: str
    severity: Severity
    title: str
    description: str
    event_ids: List[str]
    vehicle_id: str
    source_ecu: ECUSource
    detected_at: datetime
    llm_explanation: Optional[str] = None
    schema_version: str = SCHEMA_VERSION
