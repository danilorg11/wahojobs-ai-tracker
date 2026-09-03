"""Canonical pre-v2 entry point; delegates to the unchanged audited method.

This module has no benchmark, review-artifact, database, or evaluation dependency.
It does not execute a provider call on import. Calls require separate authorization.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import semantic_development_core as _method

FROZEN_METHOD_VERSION = "wahojobs_semantic_ranking_method_v1"
IMPLEMENTATION_VERSION = _method.VERSION
INTEGRATION = "ties_only"

# Exact aliases: no new interpretation, filtering, schema, prompt, or HTTP policy.
prepare_request = _method.prepare_request
provider_body = _method.provider_body
call_provider = _method.call_provider
validate_output = _method.validate_output


def integrate(legacy_items, request, output, *, failure=None):
    """Apply the selected integration, never the development full-order option."""
    return _method.integrate(
        legacy_items, request, output, mode=INTEGRATION, failure=failure
    )
