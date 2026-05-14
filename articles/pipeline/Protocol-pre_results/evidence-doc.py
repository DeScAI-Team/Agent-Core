#!/usr/bin/env python3
"""Build a compact Markdown audit trail for the protocol review pipeline.

Direct reuse of empirical/evidence-doc.py — the audit generator reads
review.json generically and does not contain empirical-specific logic.
This script simply delegates to the empirical implementation with the
correct sys.path so imports resolve.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_EMPIRICAL = _BASE.parent / "empirical"

if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# Override the SCORE_EXCLUDED_GRADES import used by evidence-doc with
# the protocol version (empty frozenset — no grades excluded).
import prep as _protocol_prep  # noqa: E402
sys.modules.setdefault("prep", _protocol_prep)

from importlib import import_module  # noqa: E402
_evidence_doc = import_module("evidence-doc")

if __name__ == "__main__":
    _evidence_doc.main()
