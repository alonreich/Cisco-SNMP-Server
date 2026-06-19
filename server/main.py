import sys
sys.dont_write_bytecode = True

import eventlet
eventlet.monkey_patch()

import os
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning, module="eventlet|.*eventlet.*")
warnings.filterwarnings("ignore", message=".*EventletDeprecationWarning.*")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import json
import logging
import sqlite3
import re
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit
from flask_cors import CORS

from database.history_db import HistoryDB

CONFIG_PATH = os.path.join(PROJECT_ROOT, 'config', 'config.json')
POSITIONS_PATH = os.path.join(PROJECT_ROOT, 'config', 'topology_positions.json')

def load_config():
    defaults = {
        "server": {"host": "0.0.0.0", "port": 8000, "debug": False},
        "snmp": {"community": "public", "timeout": 2, "retries": 1},
        "monitored_switches": [],
        "history": {"retention_days": 30, "db_path": os.path.join(PROJECT_ROOT, "database", "history.db")},
        "logging": {"level": "INFO", "file": os.path.join(PROJECT_ROOT, "logs", "flask.log"), "max_size_mb": 5},
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                loaded = json.load(f)
                # Deep merge for nested dicts
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in defaults:
                        defaults[k].update(v)
                    else:
                        defaults[k] = v
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Error loading config.json: {e}")
            pass # Use defaults
    if not os.path.isabs(defaults['history']['db_path']):
        defaults['history']['db_path'] = os.path.join(PROJECT_ROOT, defaults['history']['db_path'])
    if not os.path.isabs(defaults['logging']['file']):
        defaults['logging']['file'] = os.path.join(PROJECT_ROOT, defaults['logging']['file'])
    os.makedirs(os.path.dirname(defaults['logging']['file']), exist_ok=True)
    return defaults

config = load_config()

# --- Logging Setup ---
log_file = config['logging']['file']
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# Prevent duplicate handlers if script is reloaded
if not logger.handlers:
    fh = RotatingFileHandler(log_file, maxBytes=config['logging']['max_size_mb']*1024*1024, backupCount=1)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(fh)
# ---------------------

db = HistoryDB(config['history']['db_path'])
db.init_db()

# --- App Initialization ---
app = Flask(__name__,
            template_folder=os.path.join(PROJECT_ROOT, 'ui', 'templates'),
            static_folder=os.path.join(PROJECT_ROOT, 'ui', 'static'))
app.config['TEMPLATES_AUTO_RELOAD'] = True
CORS(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")
# --------------------------

def get_fresh_device_data():
    devices = db.get_devices()
    all_interfaces_flat = []
    device_data_map = {}
    
    current_config = load_config() # Load fresh config for targets
    
    for d in devices:
        ip = d['ip']
        cpu_rows = db.get_cpu_history(ip, hours=1)
        target = next((s for s in current_config.get('monitored_switches', []) if s['ip'] == ip), {})
        
        if_hist = db.get_interface_history(ip, hours=1)
        interfaces = []
        if if_hist:
            by_idx = {}
            for row in if_hist:
                idx = row[1]
                if idx not in by_idx: by_idx[idx] = []
                by_idx[idx].append(row)
            
            for idx, samples in by_idx.items():
                if len(samples) < 2: continue
                samples.sort(key=lambda x: x[0])
                curr = samples[-1]
                prev = samples[-2]
                
                try:
                    speed = int(curr[10]) if curr[10] else 0
                    t1 = datetime.fromisoformat(prev[0])
                    t2 = datetime.fromisoformat(curr[0])
                    dt = (t2 - t1).total_seconds()
                    
                    in_mbps = 0
                    out_mbps = 0
                    if dt > 0:
                        in_delta = curr[6] - prev[6] if curr[6] >= prev[6] else curr[6]
                        out_delta = curr[7] - prev[7] if curr[7] >= prev[7] else curr[7]
                        in_mbps = round((in_delta * 8) / (1000000 * dt), 2)
                        out_mbps = round((out_delta * 8) / (1000000 * dt), 2)
                    
                    util = round((max(in_mbps, out_mbps) / speed * 100), 2) if speed > 0 else 0
                    iface_data = {
                        'index': idx, 'descr': curr[2], 'alias': curr[3],
                        'admin': curr[4], 'oper': curr[5], 'speed': speed,
                        'in_mbps': in_mbps, 'out_mbps': out_mbps, 'util': util,
                        'hostname': d['hostname'] or ip, 'switch_ip': ip, 'ip': ip,
                        'target_type': target.get('target_type', 'cisco')
                    }
                    interfaces.append(iface_data)
                    if curr[5] == 'up' and speed > 0: all_interfaces_flat.append(iface_data)
                except (ValueError, TypeError, ZeroDivisionError) as e:
                    logger.warning(f"Could not process interface data for {ip} if_index {idx}: {e}")

        device_data_map[ip] = {
            **d,
            'cpu': cpu_rows[-1][1] if cpu_rows else 0,
            'target_type': target.get('target_type', 'cisco'),
            'interfaces': interfaces
        }
    
    all_interfaces_flat.sort(key=lambda x: x['util'], reverse=True)
    return device_data_map, all_interfaces_flat

def save_node_positions(positions):
    try:
        with open(POSITIONS_PATH, 'w') as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to write positions file: {e}")

def load_node_positions():
    if os.path.exists(POSITIONS_PATH):
        try:
            with open(POSITIONS_PATH, 'r') as f: return json.load(f)
        except Exception: return {}
    return {}

def write_config_safely(new_config):
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(new_config, f, indent=2)
        # Update the global config variable after a successful write
        global config
        config = new_config
        return True
    except Exception as e:
        logger.error(f"Failed to write config file: {e}")
        return False

# --- Routes and API Endpoints ---
@app.after_request
def add_header(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r

@app.route('/')
def root_redirect():
    return redirect(url_for('dashboard'))

@app.route('/dashboard/')
def dashboard():
    devices, leaderboard = get_fresh_device_data()
    return render_template('index.html', devices=devices, leaderboard=leaderboard)

@app.route('/topology')
def topology():
    devices, _ = get_fresh_device_data()
    return render_template('topology.html', devices=devices, positions=load_node_positions())

# ... (Other page routes like troubleshooting, cpu, memory)
@app.route('/troubleshooting')
def troubleshooting():
    devices, _ = get_fresh_device_data()
    issues = run_diagnostics(devices)
    return render_template('troubleshooting.html', detected_issues=issues)

@app.route('/cpu')
def cpu_monitor():
    devices, _ = get_fresh_device_data()
    return render_template('cpu_v2.html', devices=devices)

@app.route('/memory')
def memory_monitor():
    devices, _ = get_fresh_device_data()
    return render_template('memory.html', devices=devices)

@app.route('/config-history')
def config_history():
    devices, _ = get_fresh_device_data()
    all_configs = []
    for ip in devices:
        hist = db.get_config_history(ip, limit=5)
        for c in hist:
            c['hostname'] = devices[ip].get('hostname') or ip
            all_configs.append(c)
    all_configs.sort(key=lambda x: x['timestamp'], reverse=True)
    return render_template('config_history.html', configs=all_configs, devices=devices)

@app.route('/devices', methods=['GET'])
def devices_page():
    latest_config = load_config()
    # Build detected hostname + status map from DB
    all_devices = db.get_devices()
    detected_hostnames = {}
    device_status = {}
    for d in all_devices:
        ip = d.get('ip', '')
        detected_hostnames[ip] = d.get('hostname') or ''
        device_status[ip] = {
            'status': d.get('status', 'unknown'),
            'last_seen': d.get('last_seen', ''),
            'sys_descr': d.get('sys_descr', '')
        }
    return render_template('devices.html',
                           devices=latest_config.get('monitored_switches', []),
                           detected_hostnames=detected_hostnames,
                           device_status=device_status)

@app.route('/api/devices', methods=['POST'])
def save_device():
    data = request.json
    if not data or not data.get('ip'):
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400
    
    # ── Smart type auto-correction ──
    # If the device is already in the DB, check its detected sys_descr/hostname
    # against the user-selected target_type and correct if clearly wrong.
    ip = data['ip']
    db_devices = db.get_devices()
    db_match = next((d for d in db_devices if d.get('ip') == ip), None)
    corrected_type = None
    if db_match:
        sys_descr = (db_match.get('sys_descr') or '').lower()
        hostname = (db_match.get('hostname') or '').lower()
        selected_type = data.get('target_type', 'cisco')
        
        # Detect VMware ESXi
        if 'vmware' in sys_descr or 'esxi' in sys_descr or 'esxi' in hostname:
            if selected_type == 'cisco':
                data['target_type'] = 'esxi'
                corrected_type = 'esxi'
        # Detect VMware VCSA / vCenter / Virtual Center
        elif 'vcenter' in sys_descr or 'vcenter' in hostname or 'virtualcenter' in sys_descr or 'vcsa' in hostname:
            if selected_type != 'vcsa':
                data['target_type'] = 'vcsa'
                corrected_type = 'vcsa'
        # Detect Cisco IOS / NX-OS / Catalyst
        elif 'cisco' in sys_descr or 'ios' in sys_descr or 'nx-os' in sys_descr or 'catalyst' in sys_descr:
            if selected_type in ('esxi', 'vcsa', 'vmware'):
                data['target_type'] = 'cisco'
                corrected_type = 'cisco'
    
    current_config = load_config()
    devices = current_config.get('monitored_switches', [])
    
    ip_to_update = data.pop('original_ip', data['ip'])
    
    existing_device_index = -1
    for i, device in enumerate(devices):
        if device.get('ip') == ip_to_update:
            existing_device_index = i
            break

    if existing_device_index != -1:
        devices[existing_device_index] = data
    else:
        devices.append(data)

    current_config['monitored_switches'] = devices
    
    if write_config_safely(current_config):
        resp = {'status': 'ok'}
        if corrected_type:
            resp['corrected_type'] = corrected_type
            resp['original_type'] = selected_type if 'selected_type' in dir() else ''
        return jsonify(resp)
    else:
        return jsonify({'status': 'error', 'message': 'Failed to save configuration file'}), 500

@app.route('/api/devices/<ip>', methods=['DELETE'])
def delete_device(ip):
    current_config = load_config()
    devices = current_config.get('monitored_switches', [])
    original_len = len(devices)
    devices = [d for d in devices if d.get('ip') != ip]
    
    if len(devices) == original_len:
        return jsonify({'status': 'error', 'message': 'Device not found'}), 404
        
    current_config['monitored_switches'] = devices
    
    if write_config_safely(current_config):
        # Purge all historical data from the database so the device
        # never appears on any screen after removal.
        try:
            db.purge_device(ip)
        except Exception as e:
            logger.error(f"Failed to purge device {ip} from DB: {e}")
        return jsonify({'status': 'ok'})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to save configuration file'}), 500

@app.route('/api/topology/positions', methods=['POST'])
def api_save_positions():
    save_node_positions(request.json)
    return jsonify({'status': 'ok'})

@app.route('/api/alerts')
def api_alerts():
    ip = request.args.get('ip')
    limit = int(request.args.get('limit', 50))
    return jsonify({'alerts': db.get_alerts(ip, limit=limit)})

@app.route('/api/suppressions', methods=['GET', 'POST', 'DELETE'])
def api_suppressions():
    if request.method == 'GET':
        return jsonify(db.get_suppressions())
    elif request.method == 'POST':
        data = request.json
        db.add_suppression(data['id'], data.get('family', ''), data.get('ip', ''))
        return jsonify({'status': 'ok'})
    elif request.method == 'DELETE':
        id = request.args.get('id')
        db.remove_suppression(id)
        return jsonify({'status': 'ok'})

# ... (Other API endpoints)
@app.route('/api/topology')
def api_topology():
    cfg = load_config()
    type_map = {d['ip']: d.get('target_type', 'cisco') for d in cfg.get('monitored_switches', [])}
    devices = list(db.get_devices())
    for d in devices:
        d['target_type'] = type_map.get(d.get('ip'), 'cisco')
    return jsonify({ 'links': db.get_topology(), 'devices': devices, 'port_channels': db.get_port_channels() })

@app.route('/api/device/<ip>/config')
def api_device_config(ip):
    config_type = request.args.get('type', 'running')
    history = db.get_config_history(ip, limit=20, config_type=config_type)
    return jsonify({'history': history})

@app.route('/api/device/<ip>/config/pull', methods=['POST'])
def api_device_config_pull(ip):
    # Config pulling via SNMP SET / TFTP is not yet implemented.
    # Return a clear error so the UI button shows meaningful feedback.
    return jsonify({'status': 'error', 'error': 'Live config extraction is not yet implemented. Configure SNMP config polling on the device first.'}), 501

@app.route('/api/device/<ip>/history')
def api_device_history(ip):
    metric = request.args.get('metric', 'cpu')
    hours = int(request.args.get('hours', 1))
    data = []
    if metric == 'cpu':
        data = db.get_cpu_history(ip, hours=hours)
    elif metric == 'memory':
        data = db.get_memory_history(ip, hours=hours)
    return jsonify({'data': data})
    
# --- Diagnostics ---
def run_diagnostics(devices):
    """Analyze device and interface data for real issues."""
    issues = []
    current_config = load_config()
    type_map = {d['ip']: d.get('target_type', 'cisco') for d in current_config.get('monitored_switches', [])}

    for ip, dev in devices.items():
        target_type = type_map.get(ip, dev.get('target_type', 'cisco'))
        hostname = dev.get('hostname', ip)
        status = dev.get('status', 'unknown')
        is_vmware = target_type in ('vmware', 'esxi', 'vcsa')

        # --- Offline device ---
        if status != 'online':
            issues.append({
                'title': 'Device Unreachable',
                'severity_label': 'CRITICAL',
                'severity_class': 'bg-red-500/10 border border-red-500/20 text-red-400',
                'data_severity': 'critical',
                'is_global': False,
                'target_type': target_type,
                'hostname': hostname,
                'target_device_info': f"{hostname}\nIP: {ip}\nStatus: {status.upper()}\nSNMP: No Response",
                'why_problem': 'The device is not responding to SNMP polling. This may indicate a network outage, power failure, or SNMP service failure.',
                'simple_terms': 'This device has gone dark — we cannot reach it for monitoring.',
                'solution': 'Check physical connectivity, power, and SNMP service status on the device.',
                'connection_type': 'SNMP UDP 161',
                'remediation_title': 'Verify Connectivity',
                'issue_family': 'unreachable',
                'remediation_id': f'fix_ping_{ip}',
                'remediation_script': f'ping {ip}\n# If ping fails, check physical link and power.\n# If ping works but SNMP fails, check SNMP config:\n# show snmp community   (Cisco)\n# esxcli system snmp get   (ESXi)'
            })
            continue  # Skip further checks for offline devices

        # --- High CPU ---
        cpu = dev.get('cpu')
        if cpu is not None and float(cpu) > 70:
            issues.append({
                'title': 'High CPU Utilization',
                'severity_label': 'HIGH',
                'severity_class': 'bg-orange-500/10 border border-orange-500/20 text-orange-400',
                'data_severity': 'high',
                'is_global': False,
                'target_type': target_type,
                'hostname': hostname,
                'target_device_info': f"{hostname}\nIP: {ip}\nCPU: {cpu}%\nThreshold: >70%",
                'why_problem': 'Sustained high CPU can cause packet drops, slow SNMP responses, and protocol instability (STP, routing reconvergence).',
                'simple_terms': 'The device brain is overloaded — packets and management may be delayed.',
                'solution': 'Identify the CPU-consuming process and reduce load, or consider upgrading hardware.',
                'connection_type': 'SNMP hrProcessorTable / ciscoProcessMIB',
                'remediation_title': 'Investigate CPU Usage',
                'issue_family': 'high_cpu',
                'remediation_id': f'fix_cpu_{ip}',
                'remediation_script': f'\n# Cisco: show processes cpu sorted\n# ESXi: esxtop -b -d 2 -n 5 | findstr CPU\n# VMware VCSA: Check vCenter service status via VAMI (https://{ip}:5480)\n# SNMP verify: snmpwalk -v2c -c <community> {ip} 1.3.6.1.4.1.9.9.109.1.1.1.1.3' if not is_vmware else f'\n# ESXi: esxtop -b -d 2 -n 5\n# Check VM resource contention\n# SNMP verify: snmpwalk -v2c -c <community> {ip} 1.3.6.1.2.1.25.3.3.1.2'
            })

        # --- Interface checks (Cisco only — vmware virtual switch state isn't actionable the same way) ---
        if not is_vmware:
            for iface in dev.get('interfaces', []):
                if_descr = iface.get('descr', '')
                if_alias = iface.get('alias', '')
                admin = iface.get('admin', 'up')
                oper = iface.get('oper', 'up')
                in_err = iface.get('in_errors', 0) or 0
                out_err = iface.get('out_errors', 0) or 0
                speed = iface.get('speed', 0) or 0
                descr_lower = if_descr.lower()

                # Skip stack, vlan, loopback, null for down-state checks (they're often intentionally down)
                is_logical = any(x in descr_lower for x in ['stacksub', 'stackport', 'loopback', 'null', 'vlan'])

                # --- Admin up but Oper down (physical ports only) ---
                if admin == 'up' and oper == 'down' and not is_logical:
                    port_label = if_alias or if_descr
                    issues.append({
                        'title': 'Interface Down (Admin Up / Oper Down)',
                        'severity_label': 'MEDIUM',
                        'severity_class': 'bg-yellow-500/10 border border-yellow-500/20 text-yellow-400',
                        'data_severity': 'medium',
                        'is_global': False,
                        'target_type': target_type,
                        'hostname': hostname,
                        'target_device_info': f"{hostname}\nIP: {ip}\nPort: {if_descr}\nAlias: {if_alias or '—'}\nAdmin: UP / Oper: DOWN",
                        'why_problem': 'The interface is administratively enabled but the line protocol is down. This usually means a cable, SFP, or far-end issue.',
                        'simple_terms': 'The port is turned on but nothing is on the other end of the wire (or the wire is broken).',
                        'solution': 'Check the physical cable, SFP transceiver, and the connected device. If the port is intentionally unused, consider setting it to admin down.',
                        'connection_type': 'SNMP ifOperStatus',
                        'remediation_title': 'Investigate or Disable Port',
                        'issue_family': 'interface_down',
                        'remediation_id': f'fix_ifdown_{ip}_{iface.get("index", if_descr)}',
                        'remediation_script': f'\n# Check the connected device and cable\nshow interface {if_descr}\nshow run interface {if_descr}\n# If intentionally unused, disable it:\nconf t\ninterface {if_descr}\nshutdown\nend\nwr mem'
                    })

                # --- Speed mismatch (1Gbps SFP in 10Gbps label) ---
                if speed > 0 and speed < 1000 and ('gigabit' in descr_lower or descr_lower.startswith('gi') or descr_lower.startswith('te')):
                    issues.append({
                        'title': 'Speed Mismatch (1Gbps on 10Gbps Port)',
                        'severity_label': 'CRITICAL',
                        'severity_class': 'bg-red-500/10 border border-red-500/20 text-red-400',
                        'data_severity': 'critical',
                        'is_global': False,
                        'target_type': target_type,
                        'hostname': hostname,
                        'target_device_info': f"{hostname}\nIP: {ip}\nPort: {if_descr}\nNegotiated Speed: {speed}Mbps\nExpected: ≥1000Mbps",
                        'why_problem': 'The interface is negotiating at sub-gigabit speed despite being a gigabit or higher port. May indicate a bad cable, SFP, or duplex mismatch.',
                        'simple_terms': 'A fast port is running slow — likely a cable or SFP issue.',
                        'solution': 'Check cable quality, SFP module, and speed/duplex negotiation settings.',
                        'connection_type': 'SNMP ifSpeed',
                        'remediation_title': 'Check Speed Negotiation',
                        'issue_family': 'speed_mismatch',
                        'remediation_id': f'fix_speed_{ip}_{iface.get("index", if_descr)}',
                        'remediation_script': f'\nshow interface {if_descr}\n# Verify speed and duplex\nshow run interface {if_descr}\n# If hard-set, try auto-negotiate:\nconf t\ninterface {if_descr}\nspeed auto\nduplex auto\nend\nwr mem'
                    })

                # --- Interface errors (CRC, runts, giants, etc.) ---
                if in_err > 100 or out_err > 100:
                    issues.append({
                        'title': 'Interface Input/Output Errors',
                        'severity_label': 'HIGH',
                        'severity_class': 'bg-orange-500/10 border border-orange-500/20 text-orange-400',
                        'data_severity': 'high',
                        'is_global': False,
                        'target_type': target_type,
                        'hostname': hostname,
                        'target_device_info': f"{hostname}\nIP: {ip}\nPort: {if_descr}\nInput Errors: {in_err}\nOutput Errors: {out_err}",
                        'why_problem': 'Interface errors indicate physical layer problems — CRC errors, runts, giants, or collision. Usually caused by bad cabling, SFP, or electromagnetic interference.',
                        'simple_terms': 'The port is receiving or sending corrupted data — likely a bad cable.',
                        'solution': 'Replace the cable or SFP, check for EMI, and verify duplex settings.',
                        'connection_type': 'SNMP ifInErrors / ifOutErrors',
                        'remediation_title': 'Investigate Physical Layer',
                        'issue_family': 'interface_errors',
                        'remediation_id': f'fix_err_{ip}_{iface.get("index", if_descr)}',
                        'remediation_script': f'\nshow interface {if_descr}\n# Look for: CRC, runts, giants, input/output errors\n# Try: clear counters {if_descr}\n# Monitor for new errors after clearing.\n# If errors reappear, replace cable/SFP.'
                    })

        # --- High utilization (>90%) ---
        for iface in dev.get('interfaces', []):
            util = iface.get('util', 0) or 0
            if util > 90:
                if_descr = iface.get('descr', '')
                issues.append({
                    'title': 'Interface Near Capacity (>90% Utilization)',
                    'severity_label': 'HIGH',
                    'severity_class': 'bg-orange-500/10 border border-orange-500/20 text-orange-400',
                    'data_severity': 'high',
                    'is_global': False,
                    'target_type': target_type,
                    'hostname': hostname,
                    'target_device_info': f"{hostname}\nIP: {ip}\nPort: {if_descr}\nUtilization: {util}%\nIn: {iface.get('in_mbps', 0)} Mbps / Out: {iface.get('out_mbps', 0)} Mbps",
                    'why_problem': 'This interface is saturated. Traffic above 90% utilization will cause queuing delays, jitter, and packet loss.',
                    'simple_terms': 'This pipe is almost full — traffic is backing up.',
                    'solution': 'Consider load balancing, QoS prioritization, link aggregation, or upgrading the link capacity.',
                    'connection_type': 'SNMP ifHCInOctets / ifHCOutOctets',
                    'remediation_title': 'Reduce Traffic or Upgrade Link',
                    'issue_family': 'high_utilization',
                    'remediation_id': f'fix_util_{ip}_{iface.get("index", if_descr)}',
                    'remediation_script': f'\nshow interface {if_descr}\n# Check top talkers:\nshow interface {if_descr} stats\n# Consider QoS or adding a Port-channel member'
                })

    # --- Global: SNMP community check ---
    snmp_comm = current_config.get('snmp', {}).get('community', 'public')
    if snmp_comm.lower() == 'public':
        issues.append({
            'title': 'Default SNMP Community String',
            'severity_label': 'HIGH',
            'severity_class': 'bg-yellow-500/10 border border-yellow-500/20 text-yellow-400',
            'data_severity': 'high',
            'is_global': True,
            'target_type': 'cisco',
            'hostname': 'All Devices',
            'target_device_info': 'All monitored devices\nCommunity: public\nRisk: Cleartext, well-known string',
            'why_problem': 'Using the default "public" community string allows anyone on the network to read device configuration, serial numbers, and topology data via SNMP.',
            'simple_terms': 'Your devices passwords are the factory default — anyone can read them.',
            'solution': 'Change to a strong community string and migrate to SNMPv3 where possible.',
            'connection_type': 'Global config',
            'remediation_title': 'Change SNMP Community',
            'issue_family': 'snmp_community',
            'remediation_id': 'fix_snmp_comm_global',
            'remediation_script': '\n# Cisco IOS:\nconf t\nno snmp-server community public RO\nsnmp-server community <STRONG_STRING> RO\nend\nwr mem\n\n# Better — use SNMPv3:\nconf t\nsnmp-server view V3 iso included\nsnmp-server group V3GRP v3 priv read V3\nsnmp-server user V3USER V3GRP v3 auth sha <AUTH_KEY> priv aes 128 <PRIV_KEY>\nend\nwr mem'
        })

    suppressions = db.get_suppressions()
    suppression_map = { s['remediation_id']: s for s in suppressions }
    family_suppressions = { s['issue_type'] for s in suppressions if s['remediation_id'].startswith('family:') }

    for issue in issues:
        is_suppressed = False
        if issue['remediation_id'] in suppression_map:
            is_suppressed = True
        elif f"family:{issue.get('issue_family', '')}" in family_suppressions:
            is_suppressed = True
        
        issue['is_suppressed'] = is_suppressed

    return issues

# --- Background Task ---
def background_loop():
    logger.info("Background loop started.")
    while True:
        eventlet.sleep(15)
        try:
            devices, leaderboard = get_fresh_device_data()
            diagnostics = run_diagnostics(devices)
            socketio.emit('device_update', {
                'devices': list(devices.values()),
                'leaderboard': leaderboard,
                'links': db.get_topology(),
                'port_channels': db.get_port_channels(),
                'issues': diagnostics,
                'timestamp': time.time()
            })
        except Exception as e:
            logger.error(f"Error in background_loop: {e}", exc_info=True)

if __name__ == '__main__':
    # Remove injection functions for production stability
    # inject_cpu_spikes()
    # inject_config_history()
    
    eventlet.spawn(background_loop)
    socketio.run(app, host=config['server']['host'], port=config['server']['port'], debug=config['server']['debug'])
