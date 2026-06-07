"""
Real-Time Network Bandwidth Monitoring Dashboard
FastAPI backend with async SNMPv2c polling and WebSocket broadcasting.
Windows Server compatible.
"""

from __future__ import annotations

import os
import sys

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

try:
    import cache_guard
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
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# SNMP Configuration
# ---------------------------------------------------------------------------

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

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
OID_CISCO_CIE_IF_DESCR = "1.3.6.1.4.1.9.9.195.1.2.1.1.2"
OID_CISCO_LOC_IF_DESCR = "1.3.6.1.4.1.9.2.2.1.1.28"

INTERFACE_DESCRIPTION_OIDS: tuple[tuple[str, str], ...] = (
    ("ifAlias", OID_IF_ALIAS),
    ("cieIfInterfaceDescription", OID_CISCO_CIE_IF_DESCR),
    ("locIfDescr", OID_CISCO_LOC_IF_DESCR),
)

OID_IF_HIGHSPEED = "1.3.6.1.2.1.31.1.1.1.15"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
OID_CISCO_CPU = "1.3.6.1.4.1.9.9.109.1.1.1.1.3"

SNMP_TIMEOUT = 5
SNMP_RETRIES = 2

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------

MONITORED_SWITCHES: list[dict[str, Any]] = []
DISPLAY_LIMIT: int = 10
VALID_POLLING_INTERVALS = {5, 10, 30, 60, 300, 3600}
DEFAULT_SWITCH_POLL_INTERVAL = 30
VALID_DISPLAY_LIMITS = {10, 20, 30}
MAX_MONITORED_SWITCHES = 64

GLOBAL_STATE: dict[str, dict[str, Any]] = {}
_state_lock = asyncio.Lock()

_ws_clients: set[WebSocket] = set()
_ws_lock = asyncio.Lock()
_client_focus: dict[WebSocket, str | None] = {}

_poll_event = asyncio.Event()
_polling_task: asyncio.Task | None = None
_snmp_engine: SnmpEngine | None = None

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_config_lock = asyncio.Lock()
_switch_last_poll: dict[str, float] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bandwidth-monitor")

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _safe_int(value: Any) -> int:
    if value is None: return 0
    try:
        if hasattr(value, "prettyPrint"): return int(value.prettyPrint())
        return int(value)
    except (TypeError, ValueError): return 0

def _safe_str(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, bytes): return value.decode("utf-8", errors="replace")
    return str(value)

def _is_valid_ipv4(ip: str) -> bool:
    if not ip: return False
    try:
        ipaddress.IPv4Address(ip.strip())
        return True
    except ValueError: return False

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

def _shorten_if_name(n: str) -> str:
    m = (("HundredGigabitEthernet", "Hu"), ("FortyGigabitEthernet", "Fo"), ("TwentyFiveGigE", "Twe"), ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"), ("FastEthernet", "Fa"), ("Ethernet", "Et"), ("Port-channel", "Po"), ("Vlan", "Vl"), ("Loopback", "Lo"))
    for l, s in m:
        if n.lower().startswith(l.lower()): return s + n[len(l):]
    return n

def _get_capability_speed(n: str) -> int:
    d = n.lower()
    # Strict matching for Cisco hardware types. Priority on longest match.
    if d.startswith("hu") or d.startswith("hundred"): return 100000
    if d.startswith("fo") or d.startswith("forty"): return 40000
    if d.startswith("twe") or d.startswith("twentyfive"): return 25000
    # Match 'tengig' specifically to avoid 'te' at end of 'ethernet'
    if d.startswith("te") or d.startswith("tengig"): return 10000
    if d.startswith("gi") or d.startswith("gigabit"): return 1000
    if d.startswith("fa") or d.startswith("fastether"): return 100
    return 0

# ---------------------------------------------------------------------------
# SNMP & Polling
# ---------------------------------------------------------------------------

async def _make_udp_transport(switch_ip: str) -> Any:
    target = (switch_ip, 161)
    if PYSNMP_V7:
        return await UdpTransportTarget.create(target, timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES)
    return UdpTransportTarget(target, timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES)

async def _snmp_bulk_walk(ip: str, oid: str, comm: str) -> dict[str, Any]:
    res = {}
    if not _snmp_engine: return res
    try:
        t = await _make_udp_transport(ip)
        walk = bulk_walk_cmd(_snmp_engine, CommunityData(comm), t, ContextData(), 0, 25, ObjectType(ObjectIdentity(oid)), lexicographicMode=False)
        async for (err, stat, idx, binds) in walk:
            if err or (stat and int(stat) != 0): break
            for o, v in binds:
                if not _oid_in_subtree(str(o), oid): return res
                res[_extract_ifindex(str(o), oid)] = v
    except Exception: pass
    return res

async def _walk_interface_descriptions(ip: str, comm: str) -> dict[str, str]:
    merged = {}
    for _, oid in INTERFACE_DESCRIPTION_OIDS:
        raw = await _snmp_bulk_walk(ip, oid, comm)
        for idx, val in raw.items():
            t = _safe_str(val).strip()
            if t and idx not in merged: merged[idx] = t
    return merged

async def _poll_switch(switch_ip: str, community: str) -> None:
    try:
        descr_map, alias_map, speed_map, oper_map, admin_map, in_map, out_map, cpu_map = await asyncio.gather(
            _snmp_bulk_walk(switch_ip, OID_IF_DESCR, community),
            _walk_interface_descriptions(switch_ip, community),
            _snmp_bulk_walk(switch_ip, OID_IF_HIGHSPEED, community),
            _snmp_bulk_walk(switch_ip, OID_IF_OPER_STATUS, community),
            _snmp_bulk_walk(switch_ip, OID_IF_ADMIN_STATUS, community),
            _snmp_bulk_walk(switch_ip, OID_IF_HC_IN_OCTETS, community),
            _snmp_bulk_walk(switch_ip, OID_IF_HC_OUT_OCTETS, community),
            _snmp_bulk_walk(switch_ip, OID_CISCO_CPU, community),
        )
        
        all_indices = set(descr_map) | set(alias_map) | set(speed_map) | set(oper_map) | set(in_map) | set(out_map)
        current_ts = time.time()
        cpu_usage = _safe_int(list(cpu_map.values())[0]) if cpu_map else 0
        is_reachable = len(all_indices) > 0

        async with _state_lock:
            if switch_ip not in GLOBAL_STATE: GLOBAL_STATE[switch_ip] = {"cpu_usage": 0, "interfaces": {}}
            switch_data = GLOBAL_STATE[switch_ip]
            switch_data["cpu_usage"] = cpu_usage
            switch_data["is_reachable"] = is_reachable
            ifaces = switch_data["interfaces"]
            
            seen = set()
            for idx in all_indices:
                idx_s = str(idx)
                seen.add(idx_s)
                raw_descr = _safe_str(descr_map.get(idx_s, ""))
                
                if idx_s not in ifaces:
                    ifaces[idx_s] = {
                        "prev_timestamp": current_ts,
                        "prev_in_octets": _safe_int(in_map.get(idx_s)),
                        "prev_out_octets": _safe_int(out_map.get(idx_s)),
                        "current_bps": 0.0,
                        "current_util_pct": 0.0
                    }
                
                iface = ifaces[idx_s]
                iface["ifDescr"] = _shorten_if_name(raw_descr)
                iface["ifAlias"] = _safe_str(alias_map.get(idx_s))
                iface["ifHighSpeed"] = _safe_int(speed_map.get(idx_s))
                iface["oper_status"] = _safe_int(oper_map.get(idx_s))
                iface["admin_status"] = _safe_int(admin_map.get(idx_s))
                iface["max_speed"] = _get_capability_speed(raw_descr)
                
                # Mismatch logic: Only flag if port is UP (oper_status=1)
                is_degraded = False
                if iface["oper_status"] == 1 and iface["max_speed"] > 0:
                    if iface["ifHighSpeed"] < iface["max_speed"]:
                        is_degraded = True
                        logger.info("SPEED MISMATCH: %s %s (%s) synced at %d, but cap %d", 
                                    switch_ip, idx_s, raw_descr, iface["ifHighSpeed"], iface["max_speed"])
                
                iface["is_degraded"] = is_degraded
                
                d_t = current_ts - iface["prev_timestamp"]
                if d_t > 0:
                    d_in = _safe_int(in_map.get(idx_s)) - iface["prev_in_octets"]
                    d_out = _safe_int(out_map.get(idx_s)) - iface["prev_out_octets"]
                    if d_in >= 0 and d_out >= 0:
                        iface["current_bps"] = ((d_in + d_out) * 8) / (d_t * 1_000_000)
                        iface["current_util_pct"] = (iface["current_bps"] / iface["ifHighSpeed"] * 100) if iface["ifHighSpeed"] > 0 else 0
                
                iface["prev_timestamp"] = current_ts
                iface["prev_in_octets"] = _safe_int(in_map.get(idx_s))
                iface["prev_out_octets"] = _safe_int(out_map.get(idx_s))
                
            for stale in [k for k in ifaces if k not in seen]:
                del ifaces[stale]
                
    except Exception as e:
        logger.error(f"Polling completely failed for {switch_ip}: {e}")
        async with _state_lock:
            if switch_ip not in GLOBAL_STATE: GLOBAL_STATE[switch_ip] = {"cpu_usage": 0, "interfaces": {}}
            GLOBAL_STATE[switch_ip]["is_reachable"] = False

# ---------------------------------------------------------------------------
# Data Packaging & WebSocket
# ---------------------------------------------------------------------------

async def _build_leaderboard_payload() -> list[dict[str, Any]]:
    rows = []
    async with _state_lock:
        for ip, data in GLOBAL_STATE.items():
            ifaces = data.get("interfaces", {})
            for idx, f in ifaces.items():
                if f.get("oper_status") == 1 and f.get("current_bps", 0) >= 0.5:
                    rows.append({
                        "switch_ip": ip, "ifIndex": str(idx), 
                        "ifDescr": f.get("ifDescr", ""), 
                        "ifAlias": f.get("ifAlias", ""), 
                        "ifHighSpeed": f.get("ifHighSpeed", 0), 
                        "max_speed": f.get("max_speed", 0), 
                        "is_degraded": f.get("is_degraded", False), 
                        "current_bps": round(f.get("current_bps", 0)), 
                        "current_util_pct": round(f.get("current_util_pct", 0)),
                        "debug_info": f"Cap:{f.get('max_speed')} Sync:{f.get('ifHighSpeed')}"
                    })
    rows.sort(key=lambda r: r["current_util_pct"], reverse=True)
    return rows[:DISPLAY_LIMIT]

async def _build_cpu_payload() -> list[dict[str, Any]]:
    rows = []
    async with _state_lock:
        for ip, data in GLOBAL_STATE.items(): 
            rows.append({"switch_ip": ip, "cpu_usage": int(data.get("cpu_usage", 0))})
    rows.sort(key=lambda r: r["cpu_usage"], reverse=True)
    return rows

async def _build_focus_payload(ip: str | None) -> list[dict[str, Any]] | None:
    if not ip: return None
    rows = []
    async with _state_lock:
        data = GLOBAL_STATE.get(ip)
        if not data: return []
        ifaces = data.get("interfaces", {})
        for idx, f in ifaces.items():
            rows.append({
                "switch_ip": ip, "ifIndex": str(idx), 
                "ifDescr": f.get("ifDescr", ""), 
                "ifAlias": f.get("ifAlias", ""), 
                "ifHighSpeed": f.get("ifHighSpeed", 0), 
                "max_speed": f.get("max_speed", 0), 
                "is_degraded": f.get("is_degraded", False), 
                "current_bps": round(f.get("current_bps", 0)), 
                "current_util_pct": round(f.get("current_util_pct", 0)), 
                "oper_status": f.get("oper_status", 2), 
                "admin_status": f.get("admin_status", 2)
            })
    rows.sort(key=lambda r: r["current_util_pct"], reverse=True)
    return rows

async def _broadcast_leaderboard() -> None:
    lb = await _build_leaderboard_payload()
    cpu = await _build_cpu_payload()
    async with _ws_lock:
        dead = []
        for ws in _ws_clients:
            try:
                focus_ip = _client_focus.get(ws)
                focus_data = await _build_focus_payload(focus_ip) if focus_ip else None
                await ws.send_text(json.dumps({
                    "type": "update", "leaderboard": lb, "cpu_data": cpu,
                    "focused_data": focus_data, "focused_ip": focus_ip,
                    "monitored_switches": list(MONITORED_SWITCHES), "display_limit": DISPLAY_LIMIT,
                    "timestamp": time.time()
                }))
            except Exception: dead.append(ws)
        for ws in dead:
            _ws_clients.discard(ws)
            _client_focus.pop(ws, None)

async def _polling_worker():
    while True:
        now = time.time()
        due = []
        for s in list(MONITORED_SWITCHES):
            if now - _switch_last_poll.get(s["ip"], 0) >= s["polling_interval"]:
                _switch_last_poll[s["ip"]] = now
                due.append(asyncio.create_task(_poll_switch(s["ip"], s["community"])))
        if due:
            await asyncio.gather(*due, return_exceptions=True)
            await _broadcast_leaderboard()
        try:
            await asyncio.wait_for(_poll_event.wait(), timeout=1.0)
            _poll_event.clear()
        except asyncio.TimeoutError: pass

# ---------------------------------------------------------------------------
# API & Lifespan
# ---------------------------------------------------------------------------

def _config_payload() -> dict[str, Any]:
    return {"monitored_switches": list(MONITORED_SWITCHES), "display_limit": DISPLAY_LIMIT}

async def _save_config() -> None:
    async with _config_lock:
        CONFIG_FILE.write_text(json.dumps(_config_payload(), indent=2) + "\n", encoding="utf-8")

def _load_config_from_disk() -> None:
    global MONITORED_SWITCHES, DISPLAY_LIMIT
    if not CONFIG_FILE.is_file(): return
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        DISPLAY_LIMIT = int(data.get("display_limit", 10))
        MONITORED_SWITCHES = data.get("monitored_switches", [])
    except Exception: pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _snmp_engine, _polling_task
    _snmp_engine = SnmpEngine()
    _load_config_from_disk()
    _polling_task = asyncio.create_task(_polling_worker())
    yield
    if _polling_task: _polling_task.cancel()
    if _snmp_engine: _snmp_engine.transportDispatcher.closeDispatcher()

app = FastAPI(title="SNMP Monitor", lifespan=lifespan)

@app.get("/cpu", response_class=HTMLResponse)
async def cpu_dashboard(request: Request):
    return templates.TemplateResponse(request, "cpu.html", {"monitored_switches": MONITORED_SWITCHES})

@app.get("/troubleshooting", response_class=HTMLResponse)
async def troubleshooting_page(request: Request):
    switches = [s["ip"] for s in MONITORED_SWITCHES]
    core_ip = switches[0] if switches else "10.0.0.1"
    edge_ips = switches[1:] if len(switches) > 1 else ["10.0.0.2", "10.0.0.3"]
    total_nodes = len(switches) if switches else 3
    expected_mac = "00:1A:2B:3C:4D:5E"
    
    return templates.TemplateResponse(request, "troubleshooting.html", {
        "CORE_SWITCH_IP": core_ip,
        "EDGE_SWITCH_IPS": json.dumps(edge_ips),
        "TOTAL_NODE_COUNT": total_nodes,
        "EXPECTED_ROOT_MAC": expected_mac
    })

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html", {"monitored_switches": MONITORED_SWITCHES, "display_limit": DISPLAY_LIMIT})

@app.post("/api/switches/add")
async def add_switch(body: dict):
    ip, comm, inter = body.get("ip"), body.get("community"), int(body.get("polling_interval", 30))
    if not _is_valid_ipv4(ip) or not comm: return {"status": "error"}
    for s in MONITORED_SWITCHES:
        if s["ip"] == ip:
            s.update({"community": comm, "polling_interval": inter})
            await _save_config(); await _broadcast_leaderboard()
            return {"status": "ok", **_config_payload()}
    MONITORED_SWITCHES.append({"ip": ip, "community": comm, "polling_interval": inter})
    await _save_config(); _poll_event.set(); await _broadcast_leaderboard()
    return {"status": "ok", **_config_payload()}

@app.post("/api/switches/polling-interval")
async def set_poll(body: dict):
    ip, inter = body.get("ip"), int(body.get("polling_interval", 30))
    for s in MONITORED_SWITCHES:
        if s["ip"] == ip:
            s["polling_interval"] = inter
            await _save_config(); _poll_event.set(); await _broadcast_leaderboard()
            return {"status": "ok", **_config_payload()}
    return {"status": "error"}

@app.post("/api/switches/remove")
async def rem_switch(body: dict):
    global MONITORED_SWITCHES
    ip = body.get("ip")
    MONITORED_SWITCHES = [s for s in MONITORED_SWITCHES if s["ip"] != ip]
    async with _state_lock: GLOBAL_STATE.pop(ip, None)
    await _save_config(); await _broadcast_leaderboard()
    return {"status": "ok", **_config_payload()}

@app.post("/api/config")
async def update_config_api(body: dict):
    global DISPLAY_LIMIT
    if "display_limit" in body: DISPLAY_LIMIT = int(body["display_limit"])
    await _save_config(); await _broadcast_leaderboard()
    return {"status": "ok", **_config_payload()}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    focus_ip = ws.query_params.get("focus")
    async with _ws_lock:
        _ws_clients.add(ws)
        if focus_ip: _client_focus[ws] = focus_ip
    try:
        # Initial burst with focus immediately
        lb = await _build_leaderboard_payload()
        cpu = await _build_cpu_payload()
        f_data = await _build_focus_payload(focus_ip)
        await ws.send_text(json.dumps({
            "type": "update", "leaderboard": lb, "cpu_data": cpu, 
            "focused_data": f_data, "focused_ip": focus_ip, 
            "monitored_switches": list(MONITORED_SWITCHES),
            "display_limit": DISPLAY_LIMIT, "timestamp": time.time()
        }))
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "focus_switch":
                ip = msg.get("ip")
                async with _ws_lock: _client_focus[ws] = ip
                # Instant response with full state
                lb_now = await _build_leaderboard_payload()
                cpu_now = await _build_cpu_payload()
                f_data_now = await _build_focus_payload(ip)
                await ws.send_text(json.dumps({
                    "type": "update", "leaderboard": lb_now, "cpu_data": cpu_now,
                    "focused_data": f_data_now, "focused_ip": ip,
                    "monitored_switches": list(MONITORED_SWITCHES),
                    "display_limit": DISPLAY_LIMIT, "timestamp": time.time()
                }))
    except Exception: pass
    finally:
        async with _ws_lock:
            _ws_clients.discard(ws)
            _client_focus.pop(ws, None)

@app.get("/api/troubleshoot/run")
async def run_diagnostics_api():
    anomalies = []
    rank = 1
    monitored_ips = [s["ip"] for s in MONITORED_SWITCHES]
    async with _state_lock:
        for ip in monitored_ips:
            data = GLOBAL_STATE.get(ip)
            if data and not data.get("is_reachable", True):
                anomalies.append({
                    "id": "NODE_OFFLINE",
                    "rank": rank,
                    "title": "SNMP Polling Timeout (Node Offline)",
                    "source": f"{ip} | SNMPv2c UDP/161 | Timeout",
                    "impact": "Complete loss of visibility. Switch is either powered off, disconnected, or SNMP is misconfigured.",
                    "command": f"ping {ip}\nssh {ip}",
                    "targetNode": ip,
                    "risk": "CRITICAL"
                })
                rank += 1
                continue
            
            if data:
                for idx, iface in data.get("interfaces", {}).items():
                    if iface.get("current_util_pct", 0) > 85 and iface.get("oper_status") == 1:
                        anomalies.append({
                            "id": "BCAST_STORM",
                            "rank": rank,
                            "title": "High Interface Saturation (Storm/Loop Risk)",
                            "source": f"{ip} | {iface['ifDescr']} | Util: {iface['current_util_pct']}%",
                            "impact": "Interface is heavily saturated, causing packet drops and network unresponsiveness.",
                            "command": f"interface {iface['ifDescr']}\n shutdown",
                            "targetNode": ip,
                            "risk": "HIGH"
                        })
                        rank += 1
                    elif iface.get("is_degraded", False) and iface.get("oper_status") == 1:
                        anomalies.append({
                            "id": "L2_LOOP",
                            "rank": rank,
                            "title": "Link Speed Degradation",
                            "source": f"{ip} | {iface['ifDescr']} | Sync: {iface['ifHighSpeed']} / Cap: {iface['max_speed']}",
                            "impact": "Physical layer degradation causing suboptimal throughput.",
                            "command": f"interface {iface['ifDescr']}\n speed auto\n duplex auto",
                            "targetNode": ip,
                            "risk": "MEDIUM"
                        })
                        rank += 1
    return {"anomalies": anomalies}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
