"""
Master launcher — run this file from the project root to start the monitor.

Starts the FastAPI server, begins SNMPv2c polling of configured Cisco switches,
and serves the live dashboard at http://localhost:8000

Usage (from .\\):
    master.bat   (recommended)

Do NOT run:  python main.py   (creates risk of stray caches; use master.bat)
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

try:
    import cache_guard  # noqa: F401
    from cache_guard import purge_project_caches
except ImportError:

    def purge_project_caches() -> None:
        root = Path(__file__).resolve().parent
        skip = {"venv", ".venv", "env"}
        for cache_dir in root.rglob("__pycache__"):
            if not skip.intersection(cache_dir.parts):
                shutil.rmtree(cache_dir, ignore_errors=True)

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

HOST = "0.0.0.0"
PORT = 8000


def main() -> None:
    purge_project_caches()
    print()
    print("=" * 70)
    print("  NETWORK BANDWIDTH MONITOR — Master Server")
    print("=" * 70)
    print()
    print("  SNMP engine : ACTIVE (polls switches on UDP/161 via SNMPv2c)")
    print("  Collection  : ifDescr, ifAlias, ifHighSpeed, ifHCIn/OutOctets")
    print("  Metrics     : Current Mbps + utilization % per interface")
    print("  Sorting     : Highest bandwidth usage first (Top 10 / 20 / 30)")
    print()
    print(f"  Dashboard   : http://127.0.0.1:{PORT}/")
    print(f"  Listen bind : {HOST}:{PORT}")
    print()
    print("  Use 'Add Cisco Switch' on the dashboard to build the SNMP access list.")
    print("  Allow one poll cycle after adding a switch for live Mbps data.")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 70)
    print()

    import uvicorn

    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
