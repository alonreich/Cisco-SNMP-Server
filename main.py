"""
Real-Time Network Bandwidth Monitoring Dashboard
FastAPI backend with async SNMPv2c polling and WebSocket broadcasting.
Windows Server compatible — uses standard asyncio event loop only.

Start via master.bat / master.py only — not: python main.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True

try:
    import cache_guard  # noqa: F401 — purge __pycache__ under app source
except ImportError:
    pass

import asyncio
import ipaddress
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# PySNMP: v7+ uses hlapi.v3arch.asyncio (bulk_walk_cmd); v6 lextudio uses hlapi.asyncio (bulkWalkCmd)
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
from fastapi.templating import Jinja2Templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bandwidth-monitor")

# ---------------------------------------------------------------------------
# Architectural runtime variables
# ---------------------------------------------------------------------------

# SNMP access list: only these switches are polled and shown on the dashboard.
# Each entry: {"ip": "<ipv4>", "community": "<snmpv2c read-only>"}
MONITORED_SWITCHES: list[dict[str, Any]] = []

DISPLAY_LIMIT: int = 10  # Options: 10, 20, 30 (top interfaces by utilization %)

VALID_POLLING_INTERVALS = {5, 10, 30, 60, 300, 3600}
DEFAULT_SWITCH_POLL_INTERVAL = 30
VALID_DISPLAY_LIMITS = {10, 20, 30}
MAX_MONITORED_SWITCHES = 64

# SNMP OIDs (IF-MIB)
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"  # Interface name (e.g. GigabitEthernet1/0/1)
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"  # IF-MIB admin description (preferred)
OID_CISCO_CIE_IF_DESCR = "1.3.6.1.4.1.9.9.195.1.2.1.1.2"  # CISCO-IF-EXTENSION-MIB
OID_CISCO_LOC_IF_DESCR = "1.3.6.1.4.1.9.2.2.1.1.28"  # OLD-CISCO-INTERFACES-MIB (legacy)

# Tried in order for Description column (show interfaces description text)
INTERFACE_DESCRIPTION_OIDS: tuple[tuple[str, str], ...] = (
    ("ifAlias", OID_IF_ALIAS),
    ("cieIfInterfaceDescription", OID_CISCO_CIE_IF_DESCR),
    ("locIfDescr", OID_CISCO_LOC_IF_DESCR),
)

OID_IF_HIGHSPEED = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"

SNMP_TIMEOUT = 5
SNMP_RETRIES = 2

# ---------------------------------------------------------------------------
# GLOBAL_STATE — thread-safe via asyncio.Lock
# Structure:
# {
#     "switch_ip": {
#         "ifIndex": {
#             "ifDescr": str,   # SNMP interface name
#             "ifAlias": str,   # SNMP admin description (Cisco "description" on interface)
#             "ifHighSpeed": int,
#             "prev_timestamp": float,
#             "prev_in_octets": int,
#             "prev_out_octets": int,
#             "current_bps": float,
#             "current_util_pct": float,
#         }
#     }
# }
# ---------------------------------------------------------------------------

GLOBAL_STATE: dict[str, dict[str, dict[str, Any]]] = {}
_state_lock: asyncio.Lock | None = None

# WebSocket connection registry
_ws_clients: set[WebSocket] = set()
_ws_lock: asyncio.Lock | None = None

# Polling loop control
_poll_event = asyncio.Event()
_polling_task: asyncio.Task | None = None
_snmp_engine: SnmpEngine | None = None

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_config_lock: asyncio.Lock | None = None
_switch_last_poll: dict[str, float] = {}
_desc_snmp_warned: set[str] = set()


def _normalize_oid(oid: str) -> str:
    """Strip whitespace and leading dot from SNMP OID strings."""
    return oid.strip().lstrip(".")


def _oid_in_subtree(oid_str: str, base_oid: str) -> bool:
    """True if oid_str is base_oid or a child OID under base_oid."""
    oid = _normalize_oid(oid_str)
    base = _normalize_oid(base_oid)
    return oid == base or oid.startswith(base + ".")


def _extract_ifindex(oid: str, base_oid: str) -> str:
    """Return the ifIndex suffix from a full OID string."""
    oid = _normalize_oid(oid)
    base = _normalize_oid(base_oid)
    prefix = base + "."
    if oid.startswith(prefix):
        return oid[len(prefix) :]
    return oid.split(".")[-1]


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        if hasattr(value, "prettyPrint"):
            return int(value.prettyPrint())
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_valid_ipv4(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip.strip())
        return True
    except ValueError:
        return False


def _normalize_polling_interval(raw: Any) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SWITCH_POLL_INTERVAL
    return val if val in VALID_POLLING_INTERVALS else DEFAULT_SWITCH_POLL_INTERVAL


def _normalize_switch_entry(
    raw: Any, *, default_polling_interval: int | None = None
) -> dict[str, Any] | None:
    """Validate and normalize a switch ACL entry."""
    if not isinstance(raw, dict):
        return None
    ip = str(raw.get("ip", "")).strip()
    community = str(raw.get("community", "")).strip()
    if not _is_valid_ipv4(ip):
        return None
    if not community or not re.fullmatch(r"[^\s]{1,64}", community):
        return None
    if "polling_interval" in raw:
        polling_interval = _normalize_polling_interval(raw.get("polling_interval"))
    elif default_polling_interval is not None:
        polling_interval = _normalize_polling_interval(default_polling_interval)
    else:
        polling_interval = DEFAULT_SWITCH_POLL_INTERVAL
    return {"ip": ip, "community": community, "polling_interval": polling_interval}


def _config_payload() -> dict[str, Any]:
    return {
        "monitored_switches": list(MONITORED_SWITCHES),
        "display_limit": DISPLAY_LIMIT,
    }


def _write_config_file() -> None:
    """Persist all dashboard settings to config.json (synchronous I/O)."""
    CONFIG_FILE.write_text(
        json.dumps(_config_payload(), indent=2) + "\n",
        encoding="utf-8",
    )


async def _save_config() -> None:
    assert _config_lock is not None
    async with _config_lock:
        try:
            _write_config_file()
        except OSError as exc:
            logger.error("Failed to save %s: %s", CONFIG_FILE, exc)


def _load_config_from_disk() -> None:
    """Load persisted settings on startup (called from lifespan)."""
    global MONITORED_SWITCHES, DISPLAY_LIMIT

    if not CONFIG_FILE.is_file():
        return

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", CONFIG_FILE, exc)
        return

    if not isinstance(data, dict):
        logger.warning("Invalid %s: root must be an object", CONFIG_FILE)
        return

    legacy_global_interval = data.get("polling_interval")

    if "display_limit" in data:
        DISPLAY_LIMIT = _normalize_display_limit(data["display_limit"])

    normalized: list[dict[str, Any]] = []
    raw_switches = data.get("monitored_switches", [])
    if isinstance(raw_switches, list):
        for raw in raw_switches[:MAX_MONITORED_SWITCHES]:
            default_pi = None
            if isinstance(raw, dict) and "polling_interval" not in raw:
                default_pi = legacy_global_interval
            entry = _normalize_switch_entry(raw, default_polling_interval=default_pi)
            if entry is None:
                continue
            if any(e["ip"] == entry["ip"] for e in normalized):
                continue
            normalized.append(entry)

    MONITORED_SWITCHES = normalized
    logger.info(
        "Loaded config from %s (%d switches, display_limit=%d)",
        CONFIG_FILE.name,
        len(MONITORED_SWITCHES),
        DISPLAY_LIMIT,
    )


def _normalize_display_limit(raw: Any) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DISPLAY_LIMIT
    return val if val in VALID_DISPLAY_LIMITS else DISPLAY_LIMIT


def _switch_ips() -> set[str]:
    return {s["ip"] for s in MONITORED_SWITCHES}


async def _sync_global_state_to_acl() -> None:
    """Remove metrics for switches no longer in the access list."""
    assert _state_lock is not None
    active = _switch_ips()
    async with _state_lock:
        for stale_ip in [ip for ip in GLOBAL_STATE if ip not in active]:
            del GLOBAL_STATE[stale_ip]


async def _add_switch_to_acl(
    ip: str,
    community: str,
    polling_interval: int = DEFAULT_SWITCH_POLL_INTERVAL,
) -> dict[str, Any]:
    global MONITORED_SWITCHES

    entry = _normalize_switch_entry(
        {"ip": ip, "community": community, "polling_interval": polling_interval}
    )
    if entry is None:
        return {"status": "error", "message": "Invalid IPv4 address or SNMP community."}

    for existing in MONITORED_SWITCHES:
        if existing["ip"] == entry["ip"]:
            existing["community"] = entry["community"]
            existing["polling_interval"] = entry["polling_interval"]
            await _sync_global_state_to_acl()
            await _save_config()
            await _broadcast_leaderboard()
            return {
                "status": "ok",
                "message": f"Updated switch {entry['ip']}.",
                **_config_payload(),
            }

    if len(MONITORED_SWITCHES) >= MAX_MONITORED_SWITCHES:
        return {
            "status": "error",
            "message": f"Access list full (max {MAX_MONITORED_SWITCHES} switches).",
        }

    MONITORED_SWITCHES.append(entry)
    _switch_last_poll.pop(entry["ip"], None)
    logger.info(
        "Added switch to SNMP access list: %s (poll every %ss)",
        entry["ip"],
        entry["polling_interval"],
    )
    await _sync_global_state_to_acl()
    await _save_config()
    _poll_event.set()
    await _broadcast_leaderboard()
    return {
        "status": "ok",
        "message": f"Switch {entry['ip']} added to access list.",
        **_config_payload(),
    }


async def _remove_switch_from_acl(ip: str) -> dict[str, Any]:
    global MONITORED_SWITCHES

    ip = ip.strip()
    if not _is_valid_ipv4(ip):
        return {"status": "error", "message": "Invalid IPv4 address."}

    before = len(MONITORED_SWITCHES)
    MONITORED_SWITCHES = [s for s in MONITORED_SWITCHES if s["ip"] != ip]
    if len(MONITORED_SWITCHES) == before:
        return {"status": "error", "message": f"Switch {ip} is not in the access list."}

    _switch_last_poll.pop(ip, None)
    logger.info("Removed switch from SNMP access list: %s", ip)
    await _sync_global_state_to_acl()
    await _save_config()
    await _broadcast_leaderboard()
    return {
        "status": "ok",
        "message": f"Switch {ip} removed from access list.",
        **_config_payload(),
    }


async def _update_switch_polling_interval(ip: str, polling_interval: int) -> dict[str, Any]:
    ip = ip.strip()
    if not _is_valid_ipv4(ip):
        return {"status": "error", "message": "Invalid IPv4 address."}

    interval = _normalize_polling_interval(polling_interval)
    for entry in MONITORED_SWITCHES:
        if entry["ip"] == ip:
            entry["polling_interval"] = interval
            _switch_last_poll.pop(ip, None)
            await _save_config()
            _poll_event.set()
            await _broadcast_leaderboard()
            return {
                "status": "ok",
                "message": f"Poll interval for {ip} set to {interval}s.",
                **_config_payload(),
            }

    return {"status": "error", "message": f"Switch {ip} is not in the access list."}


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def _make_udp_transport(switch_ip: str) -> Any:
    """Build UDP transport for the installed PySNMP version."""
    target = (switch_ip, 161)
    if PYSNMP_V7:
        return await UdpTransportTarget.create(
            target,
            timeout=SNMP_TIMEOUT,
            retries=SNMP_RETRIES,
        )
    return UdpTransportTarget(target, timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES)


def _snmp_error_status_failed(error_status: Any) -> bool:
    if error_status is None:
        return False
    try:
        return int(error_status) != 0
    except (TypeError, ValueError):
        return bool(error_status)


async def _snmp_bulk_walk(
    switch_ip: str, base_oid: str, community: str
) -> dict[str, Any]:
    """Perform an async SNMP bulk-walk and return {ifIndex: value}."""
    results: dict[str, Any] = {}
    if _snmp_engine is None:
        return results

    try:
        transport = await _make_udp_transport(switch_ip)
        walk = bulk_walk_cmd(
            _snmp_engine,
            CommunityData(community),
            transport,
            ContextData(),
            0,
            25,
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        )
        async for (
            error_indication,
            error_status,
            _error_index,
            var_binds,
        ) in walk:
            if error_indication:
                logger.warning(
                    "SNMP error for %s OID %s: %s",
                    switch_ip,
                    base_oid,
                    error_indication,
                )
                break

            if _snmp_error_status_failed(error_status):
                logger.warning(
                    "SNMP status error for %s OID %s: %s",
                    switch_ip,
                    base_oid,
                    error_status,
                )
                break

            for oid, value in var_binds:
                oid_str = str(oid)
                if not _oid_in_subtree(oid_str, base_oid):
                    return results
                ifindex = _extract_ifindex(oid_str, base_oid)
                results[ifindex] = value

    except Exception as exc:
        logger.warning("SNMP walk failed for %s OID %s: %s", switch_ip, base_oid, exc)

    return results


async def _walk_interface_descriptions(
    switch_ip: str, community: str
) -> dict[str, str]:
    """
    Merge interface description strings from IF-MIB ifAlias and Cisco fallbacks.
    Matches the Description column from 'show interfaces description' when exposed via SNMP.
    """
    walk_results = await asyncio.gather(
        *[
            _snmp_bulk_walk(switch_ip, oid, community)
            for _, oid in INTERFACE_DESCRIPTION_OIDS
        ]
    )

    merged: dict[str, str] = {}
    counts: dict[str, int] = {}

    for (label, _oid), raw_map in zip(INTERFACE_DESCRIPTION_OIDS, walk_results, strict=True):
        non_empty = 0
        for ifindex, value in raw_map.items():
            text = _safe_str(value).strip()
            if not text:
                continue
            non_empty += 1
            key = str(ifindex)
            if key not in merged:
                merged[key] = text
        counts[label] = non_empty

    if merged:
        if counts.get("ifAlias", 0) == 0:
            logger.debug(
                "%s: interface descriptions from Cisco/legacy MIB (counts=%s)",
                switch_ip,
                counts,
            )
    elif switch_ip not in _desc_snmp_warned:
        _desc_snmp_warned.add(switch_ip)
        logger.warning(
            "%s: SNMP returned no interface descriptions (Description column will show '—'). "
            "CLI 'show interfaces description' uses the interface 'description' command. "
            "On IOS-XE 17 try (NOT 'snmp mib ifmib'):\n"
            "  configure terminal\n"
            "  snmp ifmib ifalias long\n"
            "  end\n"
            "Test from monitoring PC: snmp_check_descriptions.py %s <community>\n"
            "Ensure the RO community can read IF-MIB (1.3.6.1.2.1.31) or use a view with "
            "'snmp-server view <v> iso included'. Verify:\n"
            "  show snmp mib ifmib ifalias | include <interface>",
            switch_ip,
            switch_ip,
        )

    return merged


async def _poll_switch(switch_ip: str, community: str) -> None:
    """Poll all required OIDs for a single switch and update GLOBAL_STATE."""
    descr_map, desc_map, speed_map, in_map, out_map = await asyncio.gather(
        _snmp_bulk_walk(switch_ip, OID_IF_DESCR, community),
        _walk_interface_descriptions(switch_ip, community),
        _snmp_bulk_walk(switch_ip, OID_IF_HIGHSPEED, community),
        _snmp_bulk_walk(switch_ip, OID_IF_HC_IN_OCTETS, community),
        _snmp_bulk_walk(switch_ip, OID_IF_HC_OUT_OCTETS, community),
    )

    all_indices = set(descr_map) | set(desc_map) | set(speed_map) | set(in_map) | set(out_map)
    current_ts = time.time()

    assert _state_lock is not None
    async with _state_lock:
        if switch_ip not in GLOBAL_STATE:
            GLOBAL_STATE[switch_ip] = {}

        switch_state = {str(k): v for k, v in GLOBAL_STATE[switch_ip].items()}
        GLOBAL_STATE[switch_ip] = switch_state
        seen_indices: set[str] = set()

        for ifindex in all_indices:
            ifindex = str(ifindex)
            seen_indices.add(ifindex)
            if_descr = _safe_str(descr_map.get(ifindex, ""))
            if_alias = desc_map.get(ifindex, "")
            if_high_speed = _safe_int(speed_map.get(ifindex, 0))
            current_in = _safe_int(in_map.get(ifindex, 0))
            current_out = _safe_int(out_map.get(ifindex, 0))

            if ifindex not in switch_state:
                switch_state[ifindex] = {
                    "ifDescr": if_descr,
                    "ifAlias": if_alias,
                    "ifHighSpeed": if_high_speed,
                    "prev_timestamp": current_ts,
                    "prev_in_octets": current_in,
                    "prev_out_octets": current_out,
                    "current_bps": 0.0,
                    "current_util_pct": 0.0,
                }
                continue

            iface = switch_state[ifindex]
            iface["ifDescr"] = if_descr
            iface["ifAlias"] = if_alias
            iface["ifHighSpeed"] = if_high_speed

            prev_ts = iface["prev_timestamp"]
            prev_in = iface["prev_in_octets"]
            prev_out = iface["prev_out_octets"]

            delta_time = current_ts - prev_ts

            if delta_time <= 0:
                iface["prev_timestamp"] = current_ts
                iface["prev_in_octets"] = current_in
                iface["prev_out_octets"] = current_out
                continue

            delta_in = current_in - prev_in
            delta_out = current_out - prev_out

            if current_in < prev_in:
                logger.debug(
                    "%s ifIndex %s: in counter rollover, skipping delta",
                    switch_ip,
                    ifindex,
                )
                delta_in = 0
            if current_out < prev_out:
                logger.debug(
                    "%s ifIndex %s: out counter rollover, skipping delta",
                    switch_ip,
                    ifindex,
                )
                delta_out = 0

            current_mbps = ((delta_in + delta_out) * 8) / (delta_time * 1_000_000)

            if if_high_speed == 0:
                current_util_pct = 0.0
            else:
                current_util_pct = (current_mbps / if_high_speed) * 100

            iface["current_bps"] = round(current_mbps)
            iface["current_util_pct"] = round(current_util_pct)
            iface["ifHighSpeed"] = round(if_high_speed)
            iface["prev_timestamp"] = current_ts
            iface["prev_in_octets"] = current_in
            iface["prev_out_octets"] = current_out

        stale = set(switch_state.keys()) - seen_indices
        for ifindex in stale:
            del switch_state[ifindex]


async def _build_leaderboard_payload() -> list[dict[str, Any]]:
    """Flatten GLOBAL_STATE, dedupe by switch+ifIndex, sort, slice."""
    unique_rows: dict[str, dict[str, Any]] = {}

    assert _state_lock is not None
    async with _state_lock:
        for switch_ip, interfaces in GLOBAL_STATE.items():
            for ifindex, data in interfaces.items():
                ifindex_key = str(ifindex)
                row_key = f"{switch_ip}|{ifindex_key}"
                unique_rows[row_key] = {
                    "switch_ip": switch_ip,
                    "ifIndex": ifindex_key,
                    "ifDescr": data.get("ifDescr", ""),
                    "ifAlias": data.get("ifAlias", ""),
                    "ifHighSpeed": round(int(data["ifHighSpeed"])),
                    "current_bps": round(float(data["current_bps"])),
                    "current_util_pct": round(float(data["current_util_pct"])),
                }

    rows = list(unique_rows.values())
    rows.sort(key=lambda r: r["current_util_pct"], reverse=True)
    return rows[:DISPLAY_LIMIT]


async def _broadcast_leaderboard() -> None:
    """Send sorted leaderboard JSON to all connected WebSocket clients."""
    payload = await _build_leaderboard_payload()
    message = json.dumps(
        {
            "type": "leaderboard",
            "data": payload,
            "monitored_switches": list(MONITORED_SWITCHES),
            "display_limit": DISPLAY_LIMIT,
            "timestamp": time.time(),
        }
    )

    assert _ws_lock is not None
    async with _ws_lock:
        dead: list[WebSocket] = []
        for ws in _ws_clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _ws_clients.discard(ws)


async def _polling_worker() -> None:
    """Poll each switch on its own configured interval (per-switch granularity)."""
    logger.info("SNMP polling worker started (per-switch intervals)")

    while True:
        now = time.time()
        due: list[asyncio.Task[Any]] = []

        for entry in list(MONITORED_SWITCHES):
            switch_ip = entry["ip"]
            interval = int(entry.get("polling_interval", DEFAULT_SWITCH_POLL_INTERVAL))
            last = _switch_last_poll.get(switch_ip, 0.0)
            if now - last >= interval:
                _switch_last_poll[switch_ip] = now
                due.append(
                    asyncio.create_task(
                        _poll_switch(switch_ip, entry["community"]),
                        name=f"poll-{switch_ip}",
                    )
                )

        if due:
            await asyncio.gather(*due, return_exceptions=True)
            await _broadcast_leaderboard()
        elif not MONITORED_SWITCHES:
            await _broadcast_leaderboard()

        try:
            await asyncio.wait_for(_poll_event.wait(), timeout=1.0)
            _poll_event.clear()
        except asyncio.TimeoutError:
            pass


def _restart_polling_task() -> None:
    """Cancel and recreate the background polling task."""
    global _polling_task

    if _polling_task and not _polling_task.done():
        _polling_task.cancel()

    _polling_task = asyncio.create_task(_polling_worker())


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _snmp_engine, _state_lock, _ws_lock, _config_lock

    _state_lock = asyncio.Lock()
    _ws_lock = asyncio.Lock()
    _config_lock = asyncio.Lock()
    _snmp_engine = SnmpEngine()
    _load_config_from_disk()
    if not CONFIG_FILE.is_file():
        try:
            _write_config_file()
        except OSError as exc:
            logger.warning("Could not create initial %s: %s", CONFIG_FILE, exc)
    _restart_polling_task()
    logger.info("Application startup complete")
    yield
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    if _snmp_engine is not None:
        dispatcher = _snmp_engine.transportDispatcher
        if dispatcher is not None:
            dispatcher.closeDispatcher()
        _snmp_engine = None
    logger.info("Application shutdown complete")


app = FastAPI(
    title="Network Bandwidth Monitor",
    description="Real-time Cisco switch interface utilization dashboard",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "monitored_switches": MONITORED_SWITCHES,
            "display_limit": DISPLAY_LIMIT,
        },
    )


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return _config_payload()


@app.post("/api/config")
async def update_config(body: dict[str, Any]) -> dict[str, Any]:
    global MONITORED_SWITCHES, DISPLAY_LIMIT

    legacy_global_interval = body.get("polling_interval")

    if "monitored_switches" in body and isinstance(body["monitored_switches"], list):
        normalized: list[dict[str, Any]] = []
        for raw in body["monitored_switches"][:MAX_MONITORED_SWITCHES]:
            default_pi = None
            if isinstance(raw, dict) and "polling_interval" not in raw:
                default_pi = legacy_global_interval
            entry = _normalize_switch_entry(raw, default_polling_interval=default_pi)
            if entry is None:
                continue
            if any(e["ip"] == entry["ip"] for e in normalized):
                continue
            normalized.append(entry)
        MONITORED_SWITCHES = normalized
        active_ips = _switch_ips()
        for stale in [ip for ip in _switch_last_poll if ip not in active_ips]:
            _switch_last_poll.pop(stale, None)
        await _sync_global_state_to_acl()

    if "display_limit" in body:
        DISPLAY_LIMIT = _normalize_display_limit(body["display_limit"])

    await _save_config()
    _poll_event.set()
    await _broadcast_leaderboard()
    return {"status": "ok", **_config_payload()}


@app.post("/api/switches/add")
async def add_switch(body: dict[str, Any]) -> dict[str, Any]:
    return await _add_switch_to_acl(
        str(body.get("ip", "")),
        str(body.get("community", "")),
        _normalize_polling_interval(
            body.get("polling_interval", DEFAULT_SWITCH_POLL_INTERVAL)
        ),
    )


@app.post("/api/switches/polling-interval")
async def set_switch_polling_interval(body: dict[str, Any]) -> dict[str, Any]:
    return await _update_switch_polling_interval(
        str(body.get("ip", "")),
        int(body.get("polling_interval", DEFAULT_SWITCH_POLL_INTERVAL)),
    )


@app.post("/api/switches/remove")
async def remove_switch(body: dict[str, Any]) -> dict[str, Any]:
    return await _remove_switch_from_acl(str(body.get("ip", "")))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    assert _ws_lock is not None
    async with _ws_lock:
        _ws_clients.add(ws)

    try:
        initial = await _build_leaderboard_payload()
        await ws.send_text(
            json.dumps(
                {
                    "type": "leaderboard",
                    "data": initial,
                    "monitored_switches": list(MONITORED_SWITCHES),
                    "display_limit": DISPLAY_LIMIT,
                    "timestamp": time.time(),
                }
            )
        )

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "update_config":
                await update_config(msg.get("payload", {}))
            elif msg_type == "add_switch":
                result = await _add_switch_to_acl(
                    str(msg.get("ip", "")),
                    str(msg.get("community", "")),
                    _normalize_polling_interval(
                        msg.get("polling_interval", DEFAULT_SWITCH_POLL_INTERVAL)
                    ),
                )
                await ws.send_text(json.dumps({"type": "switch_acl", **result}))
            elif msg_type == "remove_switch":
                result = await _remove_switch_from_acl(str(msg.get("ip", "")))
                await ws.send_text(json.dumps({"type": "switch_acl", **result}))
            elif msg_type == "set_switch_polling_interval":
                result = await _update_switch_polling_interval(
                    str(msg.get("ip", "")),
                    int(msg.get("polling_interval", DEFAULT_SWITCH_POLL_INTERVAL)),
                )
                await ws.send_text(json.dumps({"type": "switch_acl", **result}))
            elif msg_type == "set_display_limit":
                limit = int(msg.get("value", DISPLAY_LIMIT))
                if limit in VALID_DISPLAY_LIMITS:
                    await update_config({"display_limit": limit})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket error: %s", exc)
    finally:
        if _ws_lock is not None:
            async with _ws_lock:
                _ws_clients.discard(ws)


if __name__ == "__main__":
    cache_guard.purge_project_caches()
    print("Do not run main.py directly.")
    print("Start the server with:  master.bat")
