"""
Diagnostic Anomaly Analyzer (Phase 6).

    UDS Server -> DiagnosticEvent -> DiagnosticAnalyzer -> deterministic
    rules -> AnomalyReport -> optional LLM explanation

RULES DETECT. LLM EXPLAINS. See docs/analyzer-spec.md for the full
design write-up, `analyzer/rules.py` for the deterministic rules
themselves, and `analyzer/llm_explainer.py` for the optional advisory
explanation layer (never required -- the analyzer works completely
without an LLM API key).
"""

from analyzer.analyzer import AnalyzerConfig, DiagnosticAnalyzer
from analyzer.models import AnomalyReport, Severity

__all__ = ["AnalyzerConfig", "DiagnosticAnalyzer", "AnomalyReport", "Severity"]
