"""
Master Background Orchestrator for Cisco SNMP Master.
Refactored for modern pysnmp (async) + Automated Neighbor Discovery (LLDP/CDP).
"""
import sys
sys.dont_write_bytecode = True
import asyncio
import json
import logging
import os
import sys
import time
import re
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from database.history_db import HistoryDB

CONFIG_PATH = os.path.join(PROJECT_ROOT, 'config', 'config.json')

def load_config():
    defaults = {
        "snmp": {"community": "public", "timeout": 5, "retries": 2},
        "monitored_switches": [],
        "polling": {"interval_seconds": 30},
        "history": {"retention_days": 30, "db_path": os.path.join(PROJECT_ROOT, "database", "history.db")},
        "logging": {"level": "INFO", "file": os.path.join(PROJECT_ROOT, "logs", "monitor.log"), "max_size_mb": 5},
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                loaded = json.load(f)
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in defaults: defaults[k].update(v)
                    else: defaults[k] = v
        except Exception: pass
    if not os.path.isabs(defaults['history']['db_path']):
        defaults['history']['db_path'] = os.path.join(PROJECT_ROOT, defaults['history']['db_path'])
    if not os.path.isabs(defaults['logging']['file']):
        defaults['logging']['file'] = os.path.join(PROJECT_ROOT, defaults['logging']['file'])
    return defaults

config = load_config()

log_file = config['logging']['file']
os.makedirs(os.path.dirname(log_file), exist_ok=True)
logger = logging.getLogger('master')
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(log_file, maxBytes=config['logging']['max_size_mb']*1024*1024, backupCount=1)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [master] %(message)s'))
logger.addHandler(fh)

db = HistoryDB(config['history']['db_path'])
db.init_db()

try:
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType, 
        SnmpEngine, UdpTransportTarget, get_cmd, bulk_walk_cmd,
        UsmUserData, usmHMACSHAAuthProtocol, usmHMACMD5AuthProtocol,
        usmNoAuthProtocol, usmAesCfb128Protocol, usmDESPrivProtocol,
        usmNoPrivProtocol
    )
    PYSNMP_V7 = True
except ImportError:
    from pysnmp.hlapi.asyncio import (
        CommunityData, ContextData, ObjectIdentity, ObjectType, 
        SnmpEngine, UdpTransportTarget, getCmd as get_cmd, bulkWalkCmd as bulk_walk_cmd,
        UsmUserData, usmHMACSHAAuthProtocol, usmHMACMD5AuthProtocol,
        usmNoAuthProtocol, usmAesCfb128Protocol, usmDESPrivProtocol,
        usmNoPrivProtocol
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

OID_LLDP_REM_SYSNAME = '1.0.8802.1.1.2.1.4.1.1.9'
OID_LLDP_REM_PORT = '1.0.8802.1.1.2.1.4.1.1.7'
OID_CDP_REM_DEVICE = '1.3.6.1.4.1.9.9.23.1.2.1.1.6'
OID_CDP_REM_PORT = '1.3.6.1.4.1.9.9.23.1.2.1.1.7'

OID_MEM_POOL_NAME = '1.3.6.1.4.1.9.9.48.1.1.1.2'
OID_MEM_POOL_USED = '1.3.6.1.4.1.9.9.48.1.1.1.5'
OID_MEM_POOL_FREE = '1.3.6.1.4.1.9.9.48.1.1.1.6'

IF_STATUS_MAP = {'1': 'up', '2': 'down', '3': 'testing'}

AUTH_PROT_MAP = {
    'sha': usmHMACSHAAuthProtocol,
    'md5': usmHMACMD5AuthProtocol,
    'none': usmNoAuthProtocol
}

PRIV_PROT_MAP = {
    'aes': usmAesCfb128Protocol,
    'des': usmDESPrivProtocol,
    'none': usmNoPrivProtocol
}

def get_credentials(s):
    if s.get('snmp_version') == 3 or 'username' in s:
        username = s.get('username', 'MonitorUser')
        auth_key = s.get('auth_key')
        priv_key = s.get('priv_key')
        auth_protocol = s.get('auth_protocol', 'sha').lower()
        priv_protocol = s.get('priv_protocol', 'aes').lower()
        auth_proto = AUTH_PROT_MAP.get(auth_protocol, usmHMACSHAAuthProtocol)
        priv_proto = PRIV_PROT_MAP.get(priv_protocol, usmAesCfb128Protocol)
        return UsmUserData(username, authKey=auth_key, privKey=priv_key, authProtocol=auth_proto, privProtocol=priv_proto)
    else:
        comm = s.get('community') or config['snmp']['community']
        return CommunityData(comm)

async def snmp_get(engine, ip, oid, credentials):
    target = (ip, 161)
    if PYSNMP_V7:
        transport = await UdpTransportTarget.create(target, timeout=config['snmp']['timeout'], retries=config['snmp']['retries'])
    else:
        transport = UdpTransportTarget(target, timeout=config['snmp']['timeout'], retries=config['snmp']['retries'])
    try:
        iterator = get_cmd(engine, credentials, transport, ContextData(), ObjectType(ObjectIdentity(oid)))
        if asyncio.iscoroutine(iterator): res = await iterator
        else: res = await next(iterator)
        if res[0] or res[1]: return None
        return str(res[3][0][1])
    except: return None

async def snmp_walk(engine, ip, base_oid, credentials):
    results = {}
    target = (ip, 161)
    if PYSNMP_V7:
        transport = await UdpTransportTarget.create(target, timeout=config['snmp']['timeout'], retries=config['snmp']['retries'])
    else:
        transport = UdpTransportTarget(target, timeout=config['snmp']['timeout'], retries=config['snmp']['retries'])
    try:
        walk = bulk_walk_cmd(engine, credentials, transport, ContextData(), 0, 25, ObjectType(ObjectIdentity(base_oid)), lexicographicMode=False)
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

def is_physical_port(name):
    if not name: return False
    p = name.lower()
    # Exclude logical/virtual interfaces
    if any(x in p for x in ['vlan', 'loopback', 'null', 'tunnel', 'virtual', 'management']):
        return False
    # Include physical and Port-channels
    prefixes = ['gi', 'fa', 'te', 'tw', 'fi', 'hu', 'po', 'et', 'ge', 'fe']
    return any(p.startswith(pre) for pre in prefixes)

async def poll_device(engine, s):
    ip = s['ip']
    target_type = s.get('target_type', 'cisco')
    credentials = get_credentials(s)
    try:
        sys_descr = await snmp_get(engine, ip, OID_SYSDESCR, credentials)
        if not sys_descr:
            db.update_device({'ip': ip, 'status': 'offline'})
            return

        sys_name = await snmp_get(engine, ip, OID_SYSNAME, credentials) or ip
        sys_uptime = await snmp_get(engine, ip, OID_SYSUPTIME, credentials)
        db.update_device({'ip': ip, 'status': 'online', 'sys_name': sys_name, 'hostname': sys_name, 'sys_descr': sys_descr, 'sys_uptime': sys_uptime})

        if target_type == 'vmware':
            cpu_walk = await snmp_walk(engine, ip, '1.3.6.1.2.1.25.3.3.1.2', credentials)
            cpu_vals = [float(v) for v in cpu_walk.values() if v and str(v).strip()]
            if cpu_vals:
                try: db.record_cpu(ip, sum(cpu_vals) / len(cpu_vals))
                except: pass

            storage_types, storage_descrs, storage_units, storage_sizes, storage_useds = await asyncio.gather(
                snmp_walk(engine, ip, '1.3.6.1.2.1.25.2.3.1.2', credentials),
                snmp_walk(engine, ip, '1.3.6.1.2.1.25.2.3.1.3', credentials),
                snmp_walk(engine, ip, '1.3.6.1.2.1.25.2.3.1.4', credentials),
                snmp_walk(engine, ip, '1.3.6.1.2.1.25.2.3.1.5', credentials),
                snmp_walk(engine, ip, '1.3.6.1.2.1.25.2.3.1.6', credentials)
            )
            for suffix, stype in storage_types.items():
                if '.1.3.6.1.2.1.25.2.2' in stype or 'hrStorageRam' in stype or 'memory' in storage_descrs.get(suffix, '').lower():
                    try:
                        units = int(storage_units.get(suffix, 0))
                        size = int(storage_sizes.get(suffix, 0))
                        used = int(storage_useds.get(suffix, 0))
                        name = storage_descrs.get(suffix, 'Physical Memory')
                        if units > 0 and size > 0:
                            used_bytes = used * units
                            free_bytes = (size - used) * units
                            db.record_memory(ip, name, used_bytes, free_bytes)
                    except: pass
        else:
            cpu_walk = await snmp_walk(engine, ip, OID_CPU, credentials)
            cpu_vals = [v for v in cpu_walk.values() if v and str(v).strip()]
            if cpu_vals:
                try: db.record_cpu(ip, float(cpu_vals[0]))
                except: pass

            mem_names, mem_used, mem_free = await asyncio.gather(
                snmp_walk(engine, ip, OID_MEM_POOL_NAME, credentials),
                snmp_walk(engine, ip, OID_MEM_POOL_USED, credentials),
                snmp_walk(engine, ip, OID_MEM_POOL_FREE, credentials)
            )
            for suffix, name in mem_names.items():
                try:
                    used = int(mem_used.get(suffix, 0))
                    free = int(mem_free.get(suffix, 0))
                    if used > 0 or free > 0:
                        db.record_memory(ip, name, used, free)
                except: pass

        m_tasks = [
            snmp_walk(engine, ip, OID_IF_DESCR, credentials),
            snmp_walk(engine, ip, OID_IF_ALIAS, credentials),
            snmp_walk(engine, ip, OID_IF_OPER, credentials),
            snmp_walk(engine, ip, OID_IF_ADMIN, credentials),
            snmp_walk(engine, ip, OID_IF_SPEED, credentials),
            snmp_walk(engine, ip, OID_IF_HC_IN, credentials),
            snmp_walk(engine, ip, OID_IF_HC_OUT, credentials)
        ]
        if_descrs, if_aliases, if_opers, if_admins, if_speeds, if_in, if_out = await asyncio.gather(*m_tasks)

        t_tasks = [
            snmp_walk(engine, ip, OID_LLDP_REM_SYSNAME, credentials),
            snmp_walk(engine, ip, OID_LLDP_REM_PORT, credentials),
            snmp_walk(engine, ip, OID_CDP_REM_DEVICE, credentials),
            snmp_walk(engine, ip, OID_CDP_REM_PORT, credentials),
            snmp_walk(engine, ip, '1.0.8802.1.1.2.1.3.7.1.2', credentials), # lldpLocPortIfIndex
            snmp_walk(engine, ip, '1.2.840.10006.3000.1.1.2.1.1', credentials) # dot3adAggPortSelectedAggID
        ]
        lldp_names, lldp_ports, cdp_names, cdp_ports, lldp_loc_map, po_map = await asyncio.gather(*t_tasks)

        for suffix, descr in if_descrs.items():
            admin = IF_STATUS_MAP.get(re.sub(r'\D', '', if_admins.get(suffix, '2')), 'down')
            oper = IF_STATUS_MAP.get(re.sub(r'\D', '', if_opers.get(suffix, '2')), 'down')
            db.record_interface(ip, suffix, descr, if_aliases.get(suffix, ''), admin, oper, int(if_in.get(suffix, 0)), int(if_out.get(suffix, 0)), 0, 0, if_speeds.get(suffix, '0'))

        for phys_idx, po_idx in po_map.items():
            if str(po_idx) != '0':
                db.record_port_channel(ip, phys_idx, po_idx)

        for sub_oid, r_name in lldp_names.items():
            r_port = lldp_ports.get(sub_oid)
            if r_name and r_port:
                parts = sub_oid.split('.')
                local_port_num = parts[-2] if len(parts) >= 2 else parts[0]
                local_if_index = lldp_loc_map.get(local_port_num, local_port_num)
                local_port_name = if_descrs.get(local_if_index, local_port_num)
                if is_physical_port(local_port_name) and is_physical_port(r_port):
                    db.record_neighbor(ip, local_port_name, r_name, r_port, 'lldp')

        for sub_oid, r_name in cdp_names.items():
            r_port = cdp_ports.get(cdp_names.get(sub_oid) and sub_oid) # ensure same index
            r_port = cdp_ports.get(sub_oid)
            if r_name and r_port:
                local_port_idx = sub_oid.split('.')[-2] if '.' in sub_oid else sub_oid
                local_port_name = if_descrs.get(local_port_idx, local_port_idx)
                if is_physical_port(local_port_name) and is_physical_port(r_port):
                    db.record_neighbor(ip, local_port_name, r_name, r_port, 'cdp')

    except Exception as e:
        logger.error(f"Error polling {ip}: {e}")

async def run_cycle():
    engine = SnmpEngine()
    monitored = config.get('monitored_switches', [])
    await asyncio.gather(*[poll_device(engine, s) for s in monitored])
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
