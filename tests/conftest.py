"""Make the ``zoombie`` package importable from a plain checkout.

The package lives in ``scripts/zoombie/`` (it has to sit beside ``lib/`` and
``pdf/`` so the installed layout mirrors the repo layout), so ``scripts/`` is what
belongs on ``sys.path``, not the repository root.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
