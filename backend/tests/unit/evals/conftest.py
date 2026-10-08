"""Make the repo-root ``evals`` package importable from the backend test run.

The evaluation suite lives at the repository root (``/evals``) per
``structure.md``, while the backend test suite runs from ``backend/``. Adding the
repo root to ``sys.path`` here lets ``tests/unit/evals/`` import
``evals.extraction`` without installing the package.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
