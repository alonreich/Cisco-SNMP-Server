"""
Master Background Orchestrator for Cisco SNMP Master.
Refactored for modern pysnmp (async) + Automated Neighbor Discovery (LLDP/CDP).
"""
import asyncio
import sys
sys.dont_write_bytecode = True
import json
import logging
import os
import sys
import time
import re
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# Add project directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database.history_db import HistoryDB

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
CONFIG_PATH = 'config.json'

def load_config():
    defaults = {
        "snmp": {"community": "public", "timeout": 5, "retries": 2},
        "monitored_switches": [],
        "polling": {"interval_seconds": 30},
        "history": {"retention_days": 30, "db_path": "database/history.db"},
        "logging": {"level": "INFO", "file": "logs/monitor.log", "max_size_mb": 5},
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                loaded = json.load(f)
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in defaults: defaults[k].update(v)
                    else: defaults[k] = v
        except Exception: pass
    return defaults

config = load_config()

# ─────────────────────────────────────────────────────────────────────────────
# Logging & DB
# ─────────────────────────────────────────────────────────────────────────────
log_file = config['logging']['file']
os.makedirs(os.path.dirname(log_file), exist_ok=True)
logger = logging.getLogger('master')
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(log_file, maxBytes=config['logging']['max_size_mb']*1024*1024, backupCount=1)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [master] %(message)s'))
logger.addHandler(fh)

db = HistoryDB(config['history']['db_path'])
db.init_db()

# ─────────────────────────────────────────────────────────────────────────────
# SNMP Helpers (Async)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType, 
        SnmpEngine, UdpTransportTarget, get_cmd, bulk_walk_cmd
    )
    PYSNMP_V7 = True
except ImportError:
    from pysnmp.hlapi.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType, 
        SnmpEngine, UdpTransportTarget, getCmd as get_cmd, bulkWalkCmd as bulk_walk_cmd
    )
    PYSNMP_V7 = False

OID_SYSNAME = '1.3.6.1.2.1.1.5.0'
OID_SYSDESCR = '1.3.6.1.2.1.1.1.0'
OID_SYSUPTIME = '1.3.6.1.2.1.1.3.0'
OID_CPU = '1.3.6.1.4.1.9.9.109.1.1.1.1.8'
OID_IF_DESCR = '1.3.6.1.2.1.2.2.1.2'
OID_IF_ALIAS = '1.3.6.1.2.1.31.1.1.1.18'
OID_IF_OPER = '1.3.6.1.2.1.2.2.1.8'
OID_IF_ADMIN = '1.3.6.1.2.1.2.2.1.7'
OID_IF_SPEED = '1.3.6.1.2.1.31.1.1.1.15'
OID_IF_HC_IN = '1.3.6.1.2.1.31.1.1.1.6'
OID_IF_HC_OUT = '1.3.6.1.2.1.31.1.1.1.10'

# Neighbor Discovery OIDs
OID_LLDP_REM_SYSNAME = '1.0.8802.1.1.2.1.4.1.1.9'
OID_LLDP_REM_PORT = '1.0.8802.1.1.2.1.4.1.1.7'
OID_CDP_REM_DEVICE = '1.3.6.1.4.1.9.9.23.1.2.1.1.6'
OID_CDP_REM_PORT = '1.3.6.1.4.1.9.9.23.1.2.1.1.7'

# Memory Pool OIDs (Cisco MEMORY-POOL-MIB)
OID_MEM_POOL_NAME = '1.3.6.1.4.1.9.9.48.1.1.1.2'
OID_MEM_POOL_USED = '1.3.6.1.4.1.9.9.48.1.1.1.5'
OID_MEM_POOL_FREE = '1.3.6.1.4.1.9.9.48.1.1.1.6'

IF_STATUS_MAP = {'1': 'up', '2': 'down', '3': 'testing'}

async def snmp_get(engine, ip, oid, community):
    target = (ip, 161)
    if PYSNMP_V7:
        transport = await UdpTransportTarget.create(target, timeout=config['snmp']['timeout'], retries=config['snmp']['retries'])
    else:
        transport = UdpTransportTarget(target, timeout=config['snmp']['timeout'], retries=config['snmp']['retries'])
    try:
        iterator = get_cmd(engine, CommunityData(community), transport, ContextData(), ObjectType(ObjectIdentity(oid)))
        if asyncio.iscoroutine(iterator): res = await iterator
        else: res = await next(iterator)
        if res[0] or res[1]: return None
        return str(res[3][0][1])
    except: return None

async def snmp_walk(engine, ip, base_oid, community):
    results = {}
    target = (ip, 161)
    if PYSNMP_V7:
        transport = await UdpTransportTarget.create(target, timeout=config['snmp']['timeout'], retries=config['snmp']['retries'])
    else:
        transport = UdpTransportTarget(target, timeout=config['snmp']['timeout'], retries=config['snmp']['retries'])
    try:
        walk = bulk_walk_cmd(engine, CommunityData(community), transport, ContextData(), 0, 25, ObjectType(ObjectIdentity(base_oid)), lexicographicMode=False)
        if hasattr(walk, '__aiter__'):
            async for err, status, idx, varBindTable in walk:
                if err or status: break
                for vb in varBindTable:
                    oid_str = str(vb[0])
                    if not oid_str.startswith(base_oid): return results
                    results[oid_str.replace(base_oid + '.', '').replace(base_oid, '')] = str(vb[1])
        else:
            for err, status, idx, varBindTable in walk:
                if err or status: break
                for vb in varBindTable:
                    oid_str = str(vb[0])
                    if not oid_str.startswith(base_oid): return results
                    results[oid_str.replace(base_oid + '.', '').replace(base_oid, '')] = str(vb[1])
    except: pass
    return results

async def poll_device(engine, ip, community):
    try:
        sys_descr = await snmp_get(engine, ip, OID_SYSDESCR, community)
        if not sys_descr:
            db.update_device({'ip': ip, 'status': 'offline'})
            return

        sys_name = await snmp_get(engine, ip, OID_SYSNAME, community) or ip
        sys_uptime = await snmp_get(engine, ip, OID_SYSUPTIME, community)
        db.update_device({'ip': ip, 'status': 'online', 'sys_name': sys_name, 'hostname': sys_name, 'sys_descr': sys_descr, 'sys_uptime': sys_uptime})

        # CPU: walk the table because index varies per device (.0 vs .11 etc)
        cpu_walk = await snmp_walk(engine, ip, OID_CPU, community)
        cpu_vals = [v for v in cpu_walk.values() if v and str(v).strip()]
        if cpu_vals:
            try: db.record_cpu(ip, float(cpu_vals[0]))
            except: pass

        # Memory Pool (Cisco MEMORY-POOL-MIB)
        mem_names, mem_used, mem_free = await asyncio.gather(
            snmp_walk(engine, ip, OID_MEM_POOL_NAME, community),
            snmp_walk(engine, ip, OID_MEM_POOL_USED, community),
            snmp_walk(engine, ip, OID_MEM_POOL_FREE, community)
        )
        for suffix, name in mem_names.items():
            try:
                used = int(mem_used.get(suffix, 0))
                free = int(mem_free.get(suffix, 0))
                if used > 0 or free > 0:
                    db.record_memory(ip, name, used, free)
            except: pass

        # Metrics
        m_tasks = [
            snmp_walk(engine, ip, OID_IF_DESCR, community),
            snmp_walk(engine, ip, OID_IF_ALIAS, community),
            snmp_walk(engine, ip, OID_IF_OPER, community),
            snmp_walk(engine, ip, OID_IF_ADMIN, community),
            snmp_walk(engine, ip, OID_IF_SPEED, community),
            snmp_walk(engine, ip, OID_IF_HC_IN, community),
            snmp_walk(engine, ip, OID_IF_HC_OUT, community)
        ]
        if_descrs, if_aliases, if_opers, if_admins, if_speeds, if_in, if_out = await asyncio.gather(*m_tasks)

        # Topology
        t_tasks = [
            snmp_walk(engine, ip, OID_LLDP_REM_SYSNAME, community),
            snmp_walk(engine, ip, OID_LLDP_REM_PORT, community),
            snmp_walk(engine, ip, OID_CDP_REM_DEVICE, community),
            snmp_walk(engine, ip, OID_CDP_REM_PORT, community)
        ]
        lldp_names, lldp_ports, cdp_names, cdp_ports = await asyncio.gather(*t_tasks)

        # Record interfaces
        for suffix, descr in if_descrs.items():
            admin = IF_STATUS_MAP.get(re.sub(r'\D', '', if_admins.get(suffix, '2')), 'down')
            oper = IF_STATUS_MAP.get(re.sub(r'\D', '', if_opers.get(suffix, '2')), 'down')
            db.record_interface(ip, suffix, descr, if_aliases.get(suffix, ''), admin, oper, int(if_in.get(suffix, 0)), int(if_out.get(suffix, 0)), 0, 0, if_speeds.get(suffix, '0'))

        # Record topology (LLDP)
        for sub_oid, r_name in lldp_names.items():
            r_port = lldp_ports.get(sub_oid)
            if r_name and r_port:
                # LLDP OID structure: .port_num.neighbor_idx
                local_port_idx = sub_oid.split('.')[-2] if '.' in sub_oid else sub_oid
                local_port_name = if_descrs.get(local_port_idx, local_port_idx)
                db.record_neighbor(ip, local_port_name, r_name, r_port, 'lldp')

        # Record topology (CDP)
        for sub_oid, r_name in cdp_names.items():
            r_port = cdp_ports.get(sub_oid)
            if r_name and r_port:
                # CDP OID structure: .ifIndex.neighbor_idx
                local_port_idx = sub_oid.split('.')[-2] if '.' in sub_oid else sub_oid
                local_port_name = if_descrs.get(local_port_idx, local_port_idx)
                db.record_neighbor(ip, local_port_name, r_name, r_port, 'cdp')

    except Exception as e:
        logger.error(f"Error polling {ip}: {e}")

async def run_cycle():
    engine = SnmpEngine()
    monitored = config.get('monitored_switches', [])
    await asyncio.gather(*[poll_device(engine, s['ip'], s.get('community', config['snmp']['community'])) for s in monitored])
    db.cleanup_old_records(config['history']['retention_days'])
    db.enforce_size_limit(max_mb=20)

async def main():
    logger.info("Master service started (v3.1 Discovery)")
    while True:
        start_time = time.time()
        await run_cycle()
        sleep_time = max(1, config['polling']['interval_seconds'] - (time.time() - start_time))
        await asyncio.sleep(sleep_time)

if __name__ == '__main__':
    asyncio.run(main())
