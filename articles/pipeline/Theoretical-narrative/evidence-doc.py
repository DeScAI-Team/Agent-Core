#!/usr/bin/env python3
"""Build a compact Markdown audit trail for the theoretical-narrative review pipeline.

Runs after score.py (no LLM). Reads review.json, retrieve_compare output,
screener.json, and optionally originality.json; writes evidence_audit.md.

Reuses the full build machinery from the empirical evidence-doc module with
adjusted labels and provenance references for theoretical-narrative context.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_EMPIRICAL = _BASE.parent / "empirical"
if str(_EMPIRICAL) not in sys.path:
    sys.path.insert(0, str(_EMPIRICAL))

# The empirical evidence-doc module provides all the machinery we need.
# We import its main() directly since the theoretical-narrative pipeline
# uses the same JSON schema and audit format. The only difference is
# which prep.py SCORE_EXCLUDED_GRADES is used — and since our prep.py
# has SCORE_EXCLUDED_GRADES = frozenset(), we need to ensure it's on
# the path first.
sys.path.insert(0, str(_BASE))

from importlib import import_module as _import_module

# Import the empirical evidence-doc module by filename (has a hyphen)
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "evidence_doc_empirical",
    str(_EMPIRICAL / "evidence-doc.py"),
)
_mod = _ilu.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

# Re-export the main function
main = _mod.main

if __name__ == "__main__":
    main()
