import eventlet
eventlet.monkey_patch()

import sys
import os
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning, module="eventlet|.*eventlet.*")
warnings.filterwarnings("ignore", message=".*EventletDeprecationWarning.*")

sys.dont_write_bytecode = True

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import json
import logging
import sqlite3
import re
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, request, jsonify
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
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in defaults: defaults[k].update(v)
                    else: defaults[k] = v
        except Exception: pass
    if not os.path.isabs(defaults['history']['db_path']):
        defaults['history']['db_path'] = os.path.join(PROJECT_ROOT, defaults['history']['db_path'])
    if not os.path.isabs(defaults['logging']['file']):
        defaults['logging']['file'] = os.path.join(PROJECT_ROOT, defaults['logging']['file'])
    os.makedirs(os.path.dirname(defaults['logging']['file']), exist_ok=True)
    return defaults

config = load_config()

log_file = config['logging']['file']
logger = logging.getLogger()
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(log_file, maxBytes=config['logging']['max_size_mb']*1024*1024, backupCount=1)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(fh)

db = HistoryDB(config['history']['db_path'])
db.init_db()

def get_fresh_device_data():
    devices = db.get_devices()
    all_interfaces_flat = []
    
    device_data_map = {}
    for d in devices:
        ip = d['ip']
        cpu_rows = db.get_cpu_history(ip, hours=1)
        target = next((s for s in config.get('monitored_switches', []) if s['ip'] == ip), {})
        
        if_hist = db.get_interface_history(ip, hours=1)
        interfaces = []
        if if_hist:
            by_idx = {}
            for row in if_hist:
                idx = row[1]
                if idx not in by_idx: by_idx[idx] = []
                by_idx[idx].append(row)
            
            for idx, samples in by_idx.items():
                if len(samples) < 1: continue
                samples.sort(key=lambda x: x[0])
                curr = samples[-1]
                prev = samples[-2] if len(samples) > 1 else None
                
                try:
                    speed = int(curr[10]) if curr[10] else 0
                    in_mbps = 0
                    out_mbps = 0
                    if prev:
                        t1 = datetime.fromisoformat(prev[0])
                        t2 = datetime.fromisoformat(curr[0])
                        dt = (t2 - t1).total_seconds()
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
                except: pass

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
            json.dump(positions, f)
    except Exception: pass

def load_node_positions():
    if os.path.exists(POSITIONS_PATH):
        try:
            with open(POSITIONS_PATH, 'r') as f: return json.load(f)
        except Exception: return {}
    return {}

app = Flask(__name__,
            template_folder=os.path.join(PROJECT_ROOT, 'ui', 'templates'),
            static_folder=os.path.join(PROJECT_ROOT, 'ui', 'static'))
CORS(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

@app.after_request
def add_header(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r

@app.route('/')
def index():
    devices, leaderboard = get_fresh_device_data()
    return render_template('index.html', devices=devices, config=config, leaderboard=leaderboard)

@app.route('/troubleshooting')
def troubleshooting():
    devices, _ = get_fresh_device_data()
    return render_template('troubleshooting.html', devices=devices, config=config, positions=load_node_positions())

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
    return render_template('config_history.html', configs=all_configs, devices=devices, config=config)

@app.route('/cpu')
def cpu_monitor():
    devices, _ = get_fresh_device_data()
    return render_template('cpu_v2.html', devices=devices, config=config)

@app.route('/memory')
def memory_monitor():
    devices, _ = get_fresh_device_data()
    return render_template('memory.html', devices=devices, config=config)

@app.route('/api/devices')
def api_devices():
    devices, leaderboard = get_fresh_device_data()
    return jsonify({
        'devices': list(devices.values()),
        'leaderboard': leaderboard
    })

@app.route('/api/topology/positions', methods=['POST'])
def api_save_positions():
    save_node_positions(request.json)
    return jsonify({'status': 'ok'})

@app.route('/api/topology')
def api_topology():
    return jsonify({
        'links': db.get_topology(),
        'devices': list(db.get_devices())
    })

@app.route('/api/device/<ip>/details')
def api_device_details(ip):
    devices, _ = get_fresh_device_data()
    return jsonify({
        'device': devices.get(ip, {}),
        'cpu_history': db.get_cpu_history(ip, hours=24),
        'alerts': db.get_alerts(ip, limit=10)
    })

@app.route('/api/device/<ip>/history')
def api_device_history(ip):
    metric = request.args.get('metric', 'cpu')
    hours = int(request.args.get('hours', 1))
    if metric == 'cpu':
        rows = db.get_cpu_history(ip, hours=hours)
        return jsonify({'data': rows})
    elif metric == 'memory':
        conn = db.get_connection()
        try:
            since = (datetime.now() - timedelta(hours=hours)).isoformat()
            rows = conn.execute(
                "SELECT timestamp, (used_bytes*100.0/(used_bytes+free_bytes)) as pct, pool_name, used_bytes, free_bytes "
                "FROM memory_history WHERE device_ip=? AND timestamp>? ORDER BY timestamp",
                (ip, since)
            ).fetchall()
            return jsonify({'data': [list(r) for r in rows]})
        except Exception as e:
            return jsonify({'data': [], 'error': str(e)})
        finally:
            conn.close()
    return jsonify({'data': []})

@app.route('/api/alerts')
def api_alerts():
    ip = request.args.get('ip')
    limit = int(request.args.get('limit', 50))
    return jsonify({'alerts': db.get_alerts(ip, limit=limit)})

def background_loop():
    logger.info("Background loop started.")
    while True:
        eventlet.sleep(15)
        try:
            logger.info("Fetching fresh device data...")
            devices, leaderboard = get_fresh_device_data()
            logger.info(f"Emitting {len(devices)} devices and {len(leaderboard)} leaderboard entries.")
            socketio.emit('device_update', {
                'devices': list(devices.values()),
                'leaderboard': leaderboard
            })
        except Exception as e:
            logger.error(f"Error in background_loop: {e}", exc_info=True)

if __name__ == '__main__':
    eventlet.spawn(background_loop)
    socketio.run(app, host='0.0.0.0', port=8000)
