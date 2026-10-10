"""Make the repo-root ``evals`` package importable from the backend test run.

The evaluation suite lives at the repository root (``/evals``) per
``structure.md``, while the backend test suite runs from ``backend/``. The
live-AI recording test here loads the large server-rendered fixture through the
canonical eval loader (``evals.extraction.labels.load_labeled_pages``), so it
needs the repo root on ``sys.path`` just like ``tests/unit/evals/conftest.py``
does. Adding it here lets ``tests/live_ai/`` import ``evals.extraction`` without
installing the package.

``parents[3]`` is the repo root: this file is at
``<repo>/backend/tests/live_ai/conftest.py``, so ``parents`` walks
``live_ai`` (0) → ``tests`` (1) → ``backend`` (2) → ``<repo>`` (3). (The sibling
``tests/unit/evals/conftest.py`` is one level deeper, so it uses ``parents[4]``.)
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
