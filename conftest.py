"""
Repo-wide pytest configuration (Phase 8).

Tests must never make a real Anthropic API call, whatever is set in the
developer's own shell. analyzer/llm_explainer.py reads ANTHROPIC_API_KEY
from os.environ when a caller doesn't pass `env=` explicitly, so this
autouse fixture removes it (and the related base-URL/model overrides) for
every test. Tests that exercise the "configured" path pass an explicit
`env={...}` plus a fake client instead -- see
analyzer/tests/test_llm_explainer.py.

AWS_IOT_* variables are deliberately NOT touched here: the optional AWS
IoT Core smoke test (edge_gateway/tests/test_aws_iot_integration.py) is
meant to run when a developer has provisioned AWS and exported them, and
skips on its own when they're absent (always the case in CI).
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_anthropic_credentials(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(name, raising=False)
