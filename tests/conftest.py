"""Make the package and the bundled example importable without installing them."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "checkout-flow"

for directory in (ROOT / "src", EXAMPLE):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
