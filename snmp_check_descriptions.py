"""
Test SNMP interface descriptions from the monitoring PC (not from the switch CLI).
Standalone — does not import main.py (no FastAPI/Jinja2 required).

Usage (always use venv Python):
  venv\\Scripts\\python.exe -B snmp_check_descriptions.py 10.160.4.1 BynetSec
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

SNMP_TIMEOUT = 5
SNMP_RETRIES = 2

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
OID_CISCO_CIE_IF_DESCR = "1.3.6.1.4.1.9.9.195.1.2.1.1.2"
OID_CISCO_LOC_IF_DESCR = "1.3.6.1.4.1.9.2.2.1.1.28"

DESCRIPTION_OIDS: tuple[tuple[str, str], ...] = (
    ("ifAlias", OID_IF_ALIAS),
    ("cieIfInterfaceDescription", OID_CISCO_CIE_IF_DESCR),
    ("locIfDescr", OID_CISCO_LOC_IF_DESCR),
)

PYSNMP_V7 = False
try:
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        bulk_walk_cmd,
    )

    PYSNMP_V7 = True
except ImportError:
    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        bulkWalkCmd as bulk_walk_cmd,
    )


def _normalize_oid(oid: str) -> str:
    return oid.strip().lstrip(".")


def _oid_in_subtree(oid_str: str, base_oid: str) -> bool:
    oid = _normalize_oid(oid_str)
    base = _normalize_oid(base_oid)
    return oid == base or oid.startswith(base + ".")


def _extract_ifindex(oid: str, base_oid: str) -> str:
    oid = _normalize_oid(oid)
    base = _normalize_oid(base_oid)
    prefix = base + "."
    if oid.startswith(prefix):
        return oid[len(prefix) :]
    return oid.split(".")[-1]


def _safe_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "prettyPrint"):
        return str(value.prettyPrint())
    return str(value)


def _snmp_error_status_failed(error_status: object) -> bool:
    if error_status is None:
        return False
    try:
        return int(error_status) != 0
    except (TypeError, ValueError):
        return bool(error_status)


async def _make_udp_transport(switch_ip: str) -> object:
    target = (switch_ip, 161)
    if PYSNMP_V7:
        return await UdpTransportTarget.create(
            target, timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES
        )
    return UdpTransportTarget(target, timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES)


async def snmp_bulk_walk(switch_ip: str, base_oid: str, community: str) -> dict[str, str]:
    results: dict[str, str] = {}
    engine = SnmpEngine()

    try:
        transport = await _make_udp_transport(switch_ip)
        walk = bulk_walk_cmd(
            engine,
            CommunityData(community),
            transport,
            ContextData(),
            0,
            25,
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        )
        async for error_indication, error_status, _idx, var_binds in walk:
            if error_indication:
                print(f"  SNMP error ({base_oid}): {error_indication}")
                break
            if _snmp_error_status_failed(error_status):
                print(f"  SNMP status error ({base_oid}): {error_status}")
                break
            for oid, value in var_binds:
                oid_str = str(oid)
                if not _oid_in_subtree(oid_str, base_oid):
                    return results
                results[_extract_ifindex(oid_str, base_oid)] = _safe_str(value)
    except Exception as exc:
        print(f"  Walk failed ({base_oid}): {exc}")
    finally:
        dispatcher = getattr(engine, "transport_dispatcher", None) or getattr(
            engine, "transportDispatcher", None
        )
        if dispatcher is not None:
            close = getattr(dispatcher, "close_dispatcher", None) or getattr(
                dispatcher, "closeDispatcher", None
            )
            if close:
                close()

    return results


async def merge_descriptions(switch_ip: str, community: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for label, oid in DESCRIPTION_OIDS:
        raw = await snmp_bulk_walk(switch_ip, oid, community)
        for ifindex, value in raw.items():
            text = value.strip()
            if text and ifindex not in merged:
                merged[ifindex] = text
        print(f"  {label:30} {len(raw):4} total, {sum(1 for v in raw.values() if v.strip()):4} non-empty")
    return merged


async def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: venv\\Scripts\\python.exe -B snmp_check_descriptions.py <switch_ip> <community>")
        sys.exit(1)

    switch_ip = sys.argv[1]
    community = sys.argv[2]

    print(f"Python: {sys.executable}")
    print(f"Switch: {switch_ip}  Community: {community}\n")

    descr = await snmp_bulk_walk(switch_ip, OID_IF_DESCR, community)
    print(f"ifDescr: {len(descr)} entries\nDescription OIDs:")
    merged = await merge_descriptions(switch_ip, community)

    print(f"\nMerged descriptions: {len(merged)}\n")
    shown = 0
    for ifindex, text in sorted(merged.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0):
        name = descr.get(ifindex, "").strip()
        print(f"  ifIndex {str(ifindex):>5}  {name[:28]:28}  ->  {text[:70]}")
        shown += 1
        if shown >= 8:
            break

    if not merged:
        print("\nNo descriptions via SNMP.")
        print("On switch (config): snmp ifmib ifalias long")
        print("Ensure: snmp-server view SNMP-RO iso included")
        print("        snmp-server community BynetSec RO SNMP-RO")
        sys.exit(1)

    print("\nOK — dashboard Description column should populate after master.bat restart.")


if __name__ == "__main__":
    asyncio.run(main())
