"""Shared helpers for the stage scripts.

Every script in ``scripts/`` is a separate command the student runs in turn.
They all write JSON artifacts and print a short summary, so a run can be
checked by reading a file rather than by reading a screen.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def write_json(path: Path, payload: Any) -> Path:
    """Write a JSON artifact, creating the parent directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    """Read a JSON artifact."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def banner(title: str) -> None:
    """Print a section header so the output of a run is readable."""
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
