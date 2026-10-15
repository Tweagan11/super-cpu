"""Training code (Phase 2+). Run from the repo root, e.g. `python -m training.train_bc`.

There's no separate package to install: training imports the in-repo
`bridge` package directly, so features and actions always match the bridge
they ship with. This shim puts `<repo>/bridge` on sys.path so that works
whether or not `bridge` is pip installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BRIDGE_ROOT = Path(__file__).resolve().parents[1] / "bridge"
if (_BRIDGE_ROOT / "bridge" / "__init__.py").exists() and str(_BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_ROOT))
