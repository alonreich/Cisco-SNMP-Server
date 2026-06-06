"""
Project-wide cache policy: no __pycache__, no .pyc, no tool caches under app source.
Imported first by master.py and main.py (including uvicorn loading main:app).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

_ROOT = Path(__file__).resolve().parent
_SKIP_PARTS = frozenset({"venv", ".venv", "env"})


def _under_app_source(path: Path) -> bool:
    return not _SKIP_PARTS.intersection(path.parts)


def purge_project_caches() -> None:
    """Delete bytecode and tool caches under SNMP-Server source (never venv)."""
    for cache_dir in _ROOT.rglob("__pycache__"):
        if _under_app_source(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)

    for pattern in ("*.pyc", "*.pyo"):
        for artifact in _ROOT.rglob(pattern):
            if _under_app_source(artifact):
                try:
                    artifact.unlink()
                except OSError:
                    pass

    for name in (".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"):
        for cache_dir in _ROOT.rglob(name):
            if _under_app_source(cache_dir):
                shutil.rmtree(cache_dir, ignore_errors=True)


purge_project_caches()
