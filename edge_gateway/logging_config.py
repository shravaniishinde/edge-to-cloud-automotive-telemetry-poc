"""
Structured (JSON-lines) logging for the Edge Gateway, with every record
carrying the gateway's session_id so a single session's ingest -> validate
-> normalize -> publish story can be reconstructed from logs alone by
filtering on one ID -- the same correlation-ID idea the ARCHITECTURE.md
system diagram describes.

Plain `logging` module, no external dependency: a small Formatter that
renders each record as one JSON object per line, plus a LoggerAdapter that
auto-attaches session_id (and a "component" label: ingestion/validation/
normalization/publish) to every call so call sites don't have to repeat
`extra={"session_id": ...}` everywhere.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

# Attributes every stdlib LogRecord already has -- anything else found on a
# record came from an `extra={...}` kwarg at the call site (e.g. can_id,
# event_id, reason, topic) and should be included in the JSON output.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord(
    "", 0, "", 0, "", (), None,
).__dict__.keys()) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(verbose: bool = False) -> None:
    """Call once, at process start, before any gateway logger is used."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[handler],
        force=True,  # override any prior basicConfig (e.g. from run_demo's simulator side)
    )


class _MergingLoggerAdapter(logging.LoggerAdapter):
    """
    Plain `logging.LoggerAdapter.process()` REPLACES any `extra={...}`
    passed at the call site with the adapter's own `self.extra` -- it
    does not merge them. That would silently drop per-call fields like
    `can_id` or `reason` and keep only session_id/component. This
    override merges call-site extras on top of the adapter's own, so
    both survive.
    """

    def process(self, msg, kwargs):
        extra = {**self.extra, **kwargs.get("extra", {})}
        kwargs["extra"] = extra
        return msg, kwargs


def get_gateway_logger(session_id: str, component: str) -> logging.LoggerAdapter:
    """
    Returns a logger that automatically attaches `session_id` and
    `component` to every record, so `logger.info("ingested frame",
    extra={"can_id": ...})` produces a JSON line with session_id/component
    already filled in without repeating them at every call site.
    """
    base_logger = logging.getLogger(f"edge_gateway.{component}")
    return _MergingLoggerAdapter(base_logger, {"session_id": session_id, "component": component})
