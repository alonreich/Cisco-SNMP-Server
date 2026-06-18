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
    return render_template('devices.html', devices=latest_config.get('monitored_switches', []))

@app.route('/api/devices', methods=['POST'])
def save_device():
    data = request.json
    if not data or not data.get('ip'):
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400
    
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
        return jsonify({'status': 'ok'})
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

# ... (Other API endpoints)
@app.route('/api/topology')
def api_topology():
    return jsonify({ 'links': db.get_topology(), 'devices': list(db.get_devices()), 'port_channels': db.get_port_channels() })

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
    # This is a placeholder for the full diagnostics logic
    return []

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
