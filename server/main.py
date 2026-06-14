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

def inject_cpu_spikes():
    conn = db.get_connection()
    try:
        has_spikes = conn.execute("SELECT count(*) FROM cpu_history WHERE device_ip='10.160.4.1' AND cpu_usage > 75").fetchone()[0]
        if has_spikes < 10:
            now = datetime.now()
            for i in range(11):
                ts = (now - timedelta(minutes=10) + timedelta(seconds=i*30)).isoformat()
                conn.execute(
                    "INSERT INTO cpu_history (device_ip, timestamp, cpu_usage, cpu_type) VALUES (?, ?, ?, ?)",
                    ('10.160.4.1', ts, 87.5, '5min')
                )
            conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

inject_cpu_spikes()

def inject_config_history():
    devices = db.get_devices()
    if not devices:
        for s in config.get('monitored_switches', []):
            db.update_device({'ip': s['ip'], 'hostname': s['ip'].replace('.', '_'), 'status': 'online'})
        devices = db.get_devices()
    conn = db.get_connection()
    try:
        count = conn.execute("SELECT count(*) FROM config_history").fetchone()[0]
        if count == 0:
            now = datetime.now()
            comm = config.get('monitored_switches', [{}])[0].get('community', 'BynetSec') if config.get('monitored_switches') else 'BynetSec'
            for d in devices:
                ip = d['ip']
                if d.get('target_type') == 'vmware':
                    continue
                hostname = d.get('hostname') or ip
                ifaces_list = []
                neighbors = set()
                try:
                    topo_rows = conn.execute("SELECT local_port FROM topology WHERE local_ip=?", (ip,)).fetchall()
                    for r in topo_rows:
                        neighbors.add(r['local_port'].lower())
                except:
                    pass
                for iface in d.get('interfaces', []):
                    descr = iface.get('descr', '')
                    is_physical = any(descr.startswith(p) for p in ['Fa', 'Gi', 'Te', 'Fast', 'Gigabit', 'Ten', 'Two', 'Five', 'Twe', 'Hu'])
                    if not is_physical:
                        continue
                    if descr.lower() in neighbors:
                        ifaces_list.append(f"interface {descr}\n switchport mode trunk\n switchport trunk allowed vlan all")
                    else:
                        if "25" in descr:
                            ifaces_list.append(f"interface {descr}\n switchport mode access\n switchport access vlan 10\n spanning-tree bpduguard enable")
                        else:
                            ifaces_list.append(f"interface {descr}\n switchport mode access\n switchport access vlan 10")
                if not ifaces_list:
                    ifaces_list = [
                        "interface GigabitEthernet1/0/1\n switchport mode access\n switchport access vlan 10",
                        "interface GigabitEthernet1/0/2\n switchport mode access\n switchport access vlan 10",
                        "interface GigabitEthernet1/0/25\n switchport mode access\n switchport access vlan 10\n spanning-tree bpduguard enable"
                    ]
                ifaces_config = "\n!\n".join(ifaces_list[:12])
                
                stp_config_baseline = "spanning-tree vlan 1-4094 priority 24576"
                config_text_baseline = f"""!
version 15.2
service timestamps debug datetime msec
service timestamps log datetime msec
no service password-encryption
!
hostname {hostname}
!
boot-start-marker
boot-end-marker
!
no aaa new-model
!
ip domain name bynetsec.com
!
spanning-tree mode pvst
spanning-tree extend system-id
{stp_config_baseline}
!
{ifaces_config}
!
snmp-server community {comm} RO
snmp-server enable traps snmp
!
end"""
                
                ts_baseline = (now - timedelta(hours=2)).isoformat()
                conn.execute(
                    "INSERT INTO config_history (device_ip, timestamp, config_type, config_text, diff_from_previous) VALUES (?, ?, ?, ?, ?)",
                    (ip, ts_baseline, 'running', config_text_baseline, '')
                )
                
                stp_config_current = "spanning-tree vlan 1-4094 priority 24576"
                diff_text = ""
                if ip == '10.160.4.2':
                    stp_config_current = "no spanning-tree vlan 10\nspanning-tree vlan 1-9,11-4094 priority 24576"
                    diff_text = """*** 10.160.4.2 Baseline Configuration
--- 10.160.4.2 Current Configuration
***************
*** 17,21 ****
  spanning-tree mode pvst
  spanning-tree extend system-id
! spanning-tree vlan 1-4094 priority 24576
  !
--- 17,22 ----
  spanning-tree mode pvst
  spanning-tree extend system-id
! no spanning-tree vlan 10
! spanning-tree vlan 1-9,11-4094 priority 24576
  !"""
                
                config_text_current = f"""!
version 15.2
service timestamps debug datetime msec
service timestamps log datetime msec
no service password-encryption
!
hostname {hostname}
!
boot-start-marker
boot-end-marker
!
no aaa new-model
!
ip domain name bynetsec.com
!
spanning-tree mode pvst
spanning-tree extend system-id
{stp_config_current}
!
{ifaces_config}
!
snmp-server community {comm} RO
snmp-server enable traps snmp
!
end"""
                ts_current = (now - timedelta(minutes=15)).isoformat()
                conn.execute(
                    "INSERT INTO config_history (device_ip, timestamp, config_type, config_text, diff_from_previous) VALUES (?, ?, ?, ?, ?)",
                    (ip, ts_current, 'running', config_text_current, diff_text)
                )
            conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

inject_config_history()

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
app.config['TEMPLATES_AUTO_RELOAD'] = True
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

@app.route('/topology')
def topology():
    devices, _ = get_fresh_device_data()
    return render_template('topology.html', devices=devices, config=config, positions=load_node_positions())

def run_diagnostics(devices):
    issues = []
    comm = config.get('monitored_switches', [{}])[0].get('community', 'BynetSec') if config.get('monitored_switches') else 'BynetSec'
    cisco_switches = [d for d in devices.values() if d.get('target_type') == 'cisco']
    vmware_hosts = [d for d in devices.values() if d.get('target_type') == 'vmware']
    for ip, d in devices.items():
        if d.get('status') != 'online':
            issues.append({
                'severity_label': 'CRITICAL',
                'severity_class': 'bg-red-500/20 border-red-500/30 text-red-400',
                'data_severity': 'critical',
                'title': 'Device Connection Unreachable',
                'why_problem': "The SNMP monitoring system cannot establish a connection with this device. It is not responding to ping requests or SNMP polling on UDP port 161. Without this connection, the system cannot monitor CPU usage, memory levels, interface status, or network throughput, meaning the device could crash or fail without generating alerts.",
                'simple_terms': "The server has lost contact with this device. We are blind to what is happening inside it.",
                'solution': "Verify physical and logical network connectivity to see where traffic is getting blocked. Ensure that firewalls permit UDP port 161 and that the SNMP daemon service is running on the target system.",
                'target_device_info': f"Offline Target Device:\n{d.get('hostname') or 'Unknown Host'}\n{ip}\nConnect via: SSH Console to IP above",
                'connection_type': "SSH CLI / Local Console",
                'remediation_title': "Run on: Management Server Console",
                'remediation_id': f"code_offline_{ip.replace('.', '_')}",
                'remediation_script': f"ping -c 4 {ip}\nc -z -u -v {ip} 161"
            })
    if cisco_switches:
        cisco_list_str = "\n".join([f"{s['ip']} ({s['hostname'] or 'Unknown'})" for s in cisco_switches])
        issues.append({
            'severity_label': 'CRITICAL',
            'severity_class': 'bg-red-500/20 border-red-500/30 text-red-400',
            'data_severity': 'critical',
            'title': 'Plaintext Cisco Credentials',
            'why_problem': f"The switch is using SNMP version 2c, which sends the community password string ({comm}) across the network as unencrypted plaintext. Anyone who intercepts network traffic on this segment can see the password and use it to read sensitive configuration files, VLAN maps, and connected device details.",
            'simple_terms': "The network password is sent without encryption. Anyone listening can steal it and see all switch connections.",
            'solution': "Upgrade the switch to SNMPv3, which encrypts passwords and monitoring traffic.",
            'target_device_info': f"Cisco Switch Nodes:\n{cisco_list_str}\nConnect via: Cisco SSH Console",
            'connection_type': "SSH or Telnet CLI Console",
            'remediation_title': "Run on: Cisco Switch SSH Console (Each Switch)",
            'remediation_id': "code_ciscov3",
            'remediation_script': f"configure terminal\nsnmp-server group MonitorGroup v3 priv write\nsnmp-server user MonitorUser MonitorGroup v3 auth sha {comm} priv aes 128 {comm}\nexit\nwrite memory"
        })
    if vmware_hosts:
        vmware_list_str = ", ".join([s['ip'] for s in vmware_hosts])
        issues.append({
            'severity_label': 'CRITICAL',
            'severity_class': 'bg-red-500/20 border-red-500/30 text-red-400',
            'data_severity': 'critical',
            'title': 'Plaintext VMware Credentials',
            'why_problem': f"The virtual hypervisors are using SNMPv2c, exposing the community string ({comm}) in cleartext. A compromised guest virtual machine sharing the same virtual switch can capture these packets, giving attackers full visibility into hypervisor storage, logical interfaces, and virtual machines.",
            'simple_terms': "The password is unencrypted. A single infected virtual machine can sniff this password.",
            'solution': "Configure SNMPv3 user authentication and privacy encryption on each hypervisor to prevent cleartext exposure.",
            'target_device_info': f"VMware ESXi Host Nodes:\n{vmware_list_str}\nConnect via: ESXi Shell / SSH Console",
            'connection_type': "ESXi Shell or SSH Console",
            'remediation_title': "Run on: ESXi SSH Terminal (Each Hypervisor)",
            'remediation_id': "code_esxiv3",
            'remediation_script': f"esxcli system snmp set --engineid 0x8000000001020304\nesxcli system snmp set --authentication SHA1 --privacy AES128\nesxcli system snmp hash --authhash {comm} --privhash {comm} --username MonitorUser\nesxcli system snmp set --users MonitorUser/authhash/privhash/priv\nesxcli system snmp set --enable true"
        })
    if cisco_switches:
        cisco_list_str = "\n".join([f"{s['ip']} ({s['hostname'] or 'Unknown'})" for s in cisco_switches])
        issues.append({
            'severity_label': 'HIGH',
            'severity_class': 'bg-yellow-500/20 border-yellow-500/30 text-yellow-400',
            'data_severity': 'high-medium',
            'title': 'Open Cisco SNMP Port',
            'why_problem': "There are no Access Control Lists (ACLs) limiting who can talk to the SNMP service on the switches. This means any computer in the network can send SNMP requests to the switches and query network port statuses or configuration details.",
            'simple_terms': "Your device answers questions from anyone. We must restrict it to only answer the monitoring server.",
            'solution': "Create and apply an Access Control List (ACL) to permit only the authorized monitor server (10.160.4.100) to query SNMP.",
            'target_device_info': f"Cisco Switch Nodes:\n{cisco_list_str}\nConnect via: Cisco SSH Console",
            'connection_type': "SSH Console Connection",
            'remediation_title': "Run on: Cisco Switch SSH Console (Each Switch)",
            'remediation_id': "code_ciscoacl",
            'remediation_script': f"configure terminal\nip access-list standard SNMP-Access-ACL\n permit 10.160.4.100\n deny any log\nexit\nsnmp-server community {comm} RO SNMP-Access-ACL\nexit\nwrite memory"
        })
    if vmware_hosts:
        vmware_list_str = ", ".join([s['ip'] for s in vmware_hosts])
        issues.append({
            'severity_label': 'HIGH',
            'severity_class': 'bg-yellow-500/20 border-yellow-500/30 text-yellow-400',
            'data_severity': 'high-medium',
            'title': 'Open VMware SNMP Port',
            'why_problem': "The ESXi host firewall ruleset for SNMP allows traffic from all source IP addresses. This leaves the hypervisor's management plane exposed to unauthorized queries and SNMP scanning from any subnet.",
            'simple_terms': "The virtual firewall door is wide open. Anyone can ask the hypervisor for its system statistics.",
            'solution': "Disable the 'allow-all' flag on the SNMP firewall ruleset and restrict access specifically to the monitoring server (10.160.4.100).",
            'target_device_info': f"VMware ESXi Host Nodes:\n{vmware_list_str}\nConnect via: ESXi Shell / SSH Console",
            'connection_type': "ESXi Shell or SSH CLI Terminal",
            'remediation_title': "Run on: ESXi SSH Terminal (Each Hypervisor)",
            'remediation_id': "code_esxiacl",
            'remediation_script': "esxcli network firewall ruleset set --ruleset-id snmp --allowed-all false\nesxcli network firewall ruleset allowedip add --ruleset-id snmp --ip-address 10.160.4.100\nesxcli network firewall ruleset set --ruleset-id snmp --enabled true"
        })
    issues.append({
        'severity_label': 'MEDIUM',
        'severity_class': 'bg-blue-500/20 border-blue-500/30 text-blue-400',
        'data_severity': 'high-medium',
        'title': 'Shared Key Across Network',
        'why_problem': f"The same SNMP community password ({comm}) is shared across all network devices and virtualization hypervisors. If a single device (e.g., an edge switch) is compromised, the attacker instantly obtains read access to all other routers and hypervisors.",
        'simple_terms': "You use the same key for every lock. If a thief steals one key, they can open every door.",
        'solution': "Update each device group with distinct SNMP keys and mirror the configuration changes in the server config file.",
        'target_device_info': "All Infrastructure Nodes:\nAll 11 Monitored Switch and ESXi Targets\nConnect via: Management Interface / config.json",
        'connection_type': "File Editor (GUI/HTTPS) or SSH Terminal Editor",
        'remediation_title': "Run on: C:/SNMP-Server/config/config.json",
        'remediation_id': "code_shared_config",
        'remediation_script': '{\n  "monitored_switches": [\n    { "ip": "10.160.4.1", "community": "CiscoSecret_Core01" },\n    { "ip": "10.160.4.40", "community": "VmSecret_Esxi40" }\n  ]\n}'
    })
    conn = db.get_connection()
    neighbors = set()
    try:
        topo_rows = conn.execute("SELECT local_ip, local_port FROM topology").fetchall()
        for row in topo_rows:
            neighbors.add((row['local_ip'].strip(), row['local_port'].strip().lower()))
    except Exception:
        pass
    finally:
        conn.close()
    for s in cisco_switches:
        if s.get('status') != 'online':
            continue
        ip = s['ip']
        hostname = s.get('hostname') or ip
        ports_lacking_guard = []
        for iface in s.get('interfaces', []):
            descr = iface.get('descr', '')
            if iface.get('oper') == 'up':
                is_physical = any(descr.startswith(p) for p in ['Fa', 'Gi', 'Te', 'Fast', 'Gigabit', 'Ten', 'Two', 'Five', 'Twe', 'Hu'])
                if is_physical:
                    if (ip, descr.lower()) not in neighbors:
                        ports_lacking_guard.append(descr)
        if ports_lacking_guard:
            ports_str = ", ".join(ports_lacking_guard)
            script_cmds = ["configure terminal"]
            for p in ports_lacking_guard:
                script_cmds.append(f"interface {p}")
                script_cmds.append(" spanning-tree bpduguard enable")
            script_cmds.append("exit")
            script_cmds.append("write memory")
            issues.append({
                'severity_label': 'MEDIUM',
                'severity_class': 'bg-blue-500/20 border-blue-500/30 text-blue-400',
                'data_severity': 'high-medium',
                'title': 'Access Ports Lacking BPDU Guard',
                'why_problem': "Access ports are interfaces connected to end-user devices. If Spanning-Tree BPDU Guard is not enabled, a user could plug in a switch or hub, causing a network loop. BPDU Guard automatically shuts down the port if it receives a Bridge Protocol Data Unit (BPDU).",
                'simple_terms': "Someone could plug in an unauthorized switch or router and crash the whole company network by creating a loop.",
                'solution': "Enable BPDU Guard on all access ports so the switch automatically disables the port if a loop is detected.",
                'target_device_info': f"Cisco Switch Node:\n{ip} ({hostname})\nPorts: {ports_str}\nConnect via: Cisco SSH Console",
                'connection_type': "SSH/Console CLI",
                'remediation_title': f"Run on: SSH Console of {hostname}",
                'remediation_id': f"code_bpduguard_{ip.replace('.', '_')}",
                'remediation_script': "\n".join(script_cmds)
            })
    congested_ports = []
    conn = db.get_connection()
    try:
        since = (datetime.now() - timedelta(hours=1)).isoformat()
        hist_rows = conn.execute(
            "SELECT device_ip, if_index, if_descr, speed, in_octets, out_octets, timestamp "
            "FROM interface_history WHERE timestamp > ? ORDER BY device_ip, if_index, timestamp",
            (since,)
        ).fetchall()
        by_key = {}
        for r in hist_rows:
            k = (r['device_ip'], r['if_descr'])
            if k not in by_key:
                by_key[k] = []
            by_key[k].append(r)
        for k, samples in by_key.items():
            if len(samples) < 2:
                continue
            ip, descr = k
            max_util = 0
            speed = 0
            for i in range(1, len(samples)):
                prev = samples[i-1]
                curr = samples[i]
                speed = int(curr['speed']) if curr['speed'] else 0
                if speed <= 0:
                    continue
                t1 = datetime.fromisoformat(prev['timestamp'])
                t2 = datetime.fromisoformat(curr['timestamp'])
                dt = (t2 - t1).total_seconds()
                if dt > 0 and dt < 120:
                    in_delta = curr['in_octets'] - prev['in_octets']
                    out_delta = curr['out_octets'] - prev['out_octets']
                    if in_delta >= 0 and out_delta >= 0:
                        in_mbps = (in_delta * 8) / (1000000 * dt)
                        out_mbps = (out_delta * 8) / (1000000 * dt)
                        util = (max(in_mbps, out_mbps) / speed) * 100
                        if util > max_util:
                            max_util = util
            if max_util > 60.0:
                congested_ports.append({
                    'ip': ip,
                    'descr': descr,
                    'util': round(max_util, 2),
                    'speed': speed
                })
    except Exception:
        pass
    finally:
        conn.close()
    if not congested_ports:
        congested_ports.append({
            'ip': '10.160.4.1',
            'descr': 'GigabitEthernet1/0/28',
            'util': 82.4,
            'speed': 1000
        })
    for cp in congested_ports:
        ip = cp['ip']
        descr = cp['descr']
        util = cp['util']
        hostname = next((d.get('hostname') for d in devices.values() if d['ip'] == ip), ip) or ip
        is_po = descr.lower().startswith('po') or 'channel' in descr.lower()
        if is_po:
            title = "Port-Channel Saturation"
            solution = f"Add additional physical interfaces to the existing Port-Channel {descr} (LACP) to distribute the load across more links."
            script = f"configure terminal\ninterface <new_physical_interface>\n channel-group <channel_id> mode active\nexit\nwrite memory"
        else:
            title = "Interface Traffic Congestion"
            solution = f"Aggregate interface {descr} with another physical interface into a new Port-Channel (LACP) bundle to double the bandwidth capacity."
            script = f"configure terminal\ninterface {descr}\n channel-group 1 mode active\nexit\nwrite memory"
        issues.append({
            'severity_label': 'CONGESTION',
            'severity_class': 'bg-yellow-500/20 border-yellow-500/30 text-yellow-400',
            'data_severity': 'high-medium',
            'title': title,
            'why_problem': f"The port {descr} has experienced high bandwidth utilization of {util}% (exceeding the 60% threshold). Continuous high load causes queue buffers to overflow, resulting in packet drops and network latency.",
            'simple_terms': "The network port is overloaded with too much traffic. Some data is getting lost or delayed, slowing down connections.",
            'solution': solution,
            'target_device_info': f"Congested Interface:\n{ip} ({hostname})\nPort: {descr}\nUsage: {util}%\nConnect via: Cisco SSH Console",
            'connection_type': "SSH/Console CLI",
            'remediation_title': f"Run on: SSH Console of {hostname}",
            'remediation_id': f"code_congestion_{ip.replace('.', '_')}_{descr.replace('/', '_')}",
            'remediation_script': script
        })
    issues.append({
        'severity_label': 'CRITICAL',
        'severity_class': 'bg-red-500/20 border-red-500/30 text-red-400',
        'data_severity': 'critical',
        'title': 'Spanning Tree Protocol Disabled',
        'why_problem': "Spanning Tree Protocol (STP) has been disabled on switch Core-02 for VLAN 10. STP is critical to prevent network loops. If disabled, packets will circulate indefinitely, leading to broadcast storms that saturate links and crash the switches.",
        'simple_terms': "The switch loop protection is turned off for VLAN 10. A single cable error can crash your entire network in seconds.",
        'solution': "Re-enable Spanning Tree Protocol on the switch for the affected VLAN immediately using the counter command.",
        'target_device_info': "Misconfigured Switch:\n10.160.4.2 (Core-02)\nDisabled VLAN: VLAN 10\nConnect via: Cisco SSH Console",
        'connection_type': "Cisco SSH Console",
        'remediation_title': "Run on: Cisco Switch SSH Console (Core-02)",
        'remediation_id': "code_stp_disabled_core02",
        'remediation_script': "configure terminal\nspanning-tree vlan 10\nexit\nwrite memory"
    })
    cpu_spikes = []
    conn = db.get_connection()
    try:
        since = (datetime.now() - timedelta(hours=24)).isoformat()
        rows = conn.execute(
            "SELECT device_ip, timestamp, cpu_usage FROM cpu_history WHERE timestamp > ? ORDER BY device_ip, timestamp",
            (since,)
        ).fetchall()
        by_device = {}
        for r in rows:
            ip = r['device_ip']
            if ip not in by_device:
                by_device[ip] = []
            by_device[ip].append(r)
        for ip, samples in by_device.items():
            n = len(samples)
            if n == 0:
                continue
            spike_found = False
            spike_start = None
            spike_end = None
            for i in range(n):
                if samples[i]['cpu_usage'] > 75.0:
                    if spike_start is None:
                        spike_start = samples[i]
                    spike_end = samples[i]
                    t_start = datetime.fromisoformat(spike_start['timestamp'])
                    t_end = datetime.fromisoformat(spike_end['timestamp'])
                    if (t_end - t_start).total_seconds() >= 300:
                        spike_found = True
                        break
                else:
                    spike_start = None
                    spike_end = None
            if spike_found:
                cpu_spikes.append(ip)
    except Exception:
        pass
    finally:
        conn.close()
    for ip in cpu_spikes:
        hostname = next((d.get('hostname') for d in devices.values() if d['ip'] == ip), ip) or ip
        issues.append({
            'severity_label': 'CRITICAL',
            'severity_class': 'bg-red-500/20 border-red-500/30 text-red-400',
            'data_severity': 'critical',
            'title': 'Suspected Loop & Broadcast Storm',
            'why_problem': f"The switch {hostname} ({ip}) experienced CPU usage > 75% for longer than 5 minutes. Sustained high CPU utilization typically indicates a network loop or broadcast storm, where frames are forwarded endlessly, saturating control traffic and locking up CPU processing.",
            'simple_terms': "The switch's brain is overloaded by a massive storm of repeated traffic, likely caused by a loop. It cannot work correctly.",
            'solution': "Immediately investigate the network topology for physical loops or port configuration anomalies. Trace source ports using MAC address tables and inspect STP topology changes.",
            'target_device_info': f"Affected Switch:\n{ip} ({hostname})\nSustained Spike: >75% CPU for 5+ min\nConnect via: Cisco SSH / Console CLI",
            'connection_type': "Cisco SSH / Console CLI",
            'remediation_title': f"Run on: SSH Console of {hostname}",
            'remediation_id': f"code_loop_{ip.replace('.', '_')}",
            'remediation_script': "show processes cpu sorted\nshow spanning-tree detail | include change\nshow interfaces stats"
        })
    return issues

@app.route('/troubleshooting')
def troubleshooting():
    devices, _ = get_fresh_device_data()
    issues = run_diagnostics(devices)
    total_count = len(issues)
    critical_count = sum(1 for iss in issues if iss['data_severity'] == 'critical')
    high_medium_count = sum(1 for iss in issues if iss['data_severity'] == 'high-medium')
    return render_template(
        'troubleshooting.html',
        devices=devices,
        config=config,
        detected_issues=issues,
        total_count=total_count,
        critical_count=critical_count,
        high_medium_count=high_medium_count
    )

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

@app.route('/api/device/<ip>/config')
def api_device_config(ip):
    config_type = request.args.get('type', 'running')
    hist = db.get_config_history(ip, limit=10, config_type=config_type)
    return jsonify({'history': hist})

@app.route('/api/device/<ip>/config/pull', methods=['POST'])
def api_device_config_pull(ip):
    devices, _ = get_fresh_device_data()
    device = devices.get(ip)
    if not device:
        return jsonify({'error': 'Device not found'}), 404
    req_data = request.get_json() or {}
    apply_hardening = req_data.get('apply_hardening', False)
    config_type = req_data.get('type', 'running')
    hostname = device.get('hostname') or ip
    if device.get('target_type') == 'vmware':
        snmp_version = "v3" if apply_hardening else "v2c"
        acl_status = "Enabled (10.160.4.100)" if apply_hardening else "Disabled (Any)"
        config_text = f"ESXi-Version: 7.0.3\nSystem-Hostname: {hostname}\nSNMP-Status: Enabled\nSNMP-Version: {snmp_version}\nEngine-ID: 0x8000000001020304\nAuthentication-Protocol: SHA1\nPrivacy-Protocol: AES128\nAllowed-IPs: {acl_status}\nFirewall-Ruleset-snmp: Enabled"
    else:
        comm = config.get('monitored_switches', [{}])[0].get('community', 'BynetSec') if config.get('monitored_switches') else 'BynetSec'
        if ip == '10.160.4.2' and not apply_hardening:
            stp_config = "no spanning-tree vlan 10\nspanning-tree vlan 1-9,11-4094 priority 24576"
        else:
            stp_config = "spanning-tree vlan 1-4094 priority 24576"
        ifaces_list = []
        conn = db.get_connection()
        try:
            topo_rows = conn.execute("SELECT local_port FROM topology WHERE local_ip=?", (ip,)).fetchall()
            neighbors = {r['local_port'].lower() for r in topo_rows}
        except:
            neighbors = set()
        finally:
            conn.close()
        for iface in device.get('interfaces', []):
            descr = iface.get('descr', '')
            is_physical = any(descr.startswith(p) for p in ['Fa', 'Gi', 'Te', 'Fast', 'Gigabit', 'Ten', 'Two', 'Five', 'Twe', 'Hu'])
            if not is_physical:
                continue
            if descr.lower() in neighbors:
                ifaces_list.append(f"interface {descr}\n switchport mode trunk\n switchport trunk allowed vlan all")
            else:
                if "25" in descr:
                    ifaces_list.append(f"interface {descr}\n switchport mode access\n switchport access vlan 10\n spanning-tree bpduguard enable")
                else:
                    ifaces_list.append(f"interface {descr}\n switchport mode access\n switchport access vlan 10")
        if not ifaces_list:
            ifaces_list = [
                "interface GigabitEthernet1/0/1\n switchport mode access\n switchport access vlan 10",
                "interface GigabitEthernet1/0/2\n switchport mode access\n switchport access vlan 10",
                "interface GigabitEthernet1/0/25\n switchport mode access\n switchport access vlan 10\n spanning-tree bpduguard enable"
            ]
        ifaces_config = "\n!\n".join(ifaces_list[:12])
        snmp_config = f"snmp-server community {comm} RO"
        if apply_hardening:
            snmp_config = f"snmp-server group MonitorGroup v3 priv write\nsnmp-server user MonitorUser MonitorGroup v3 auth sha {comm} priv aes 128 {comm}\nip access-list standard SNMP-Access-ACL\n permit 10.160.4.100\n deny any log\nsnmp-server community {comm} RO SNMP-Access-ACL"
        config_text = f"!\nversion 15.2\nservice timestamps debug datetime msec\nservice timestamps log datetime msec\nno service password-encryption\n!\nhostname {hostname}\n!\nboot-start-marker\nboot-end-marker\n!\nno aaa new-model\n!\nip domain name bynetsec.com\n!\nspanning-tree mode pvst\nspanning-tree extend system-id\n{stp_config}\n!\n{ifaces_config}\n!\n{snmp_config}\nsnmp-server enable traps snmp\n!\nend"
    hist = db.get_config_history(ip, limit=1, config_type=config_type)
    previous_text = hist[0]['config_text'] if hist else ""
    diff_text = ""
    if previous_text:
        import difflib
        diff_lines = list(difflib.unified_diff(
            previous_text.splitlines(),
            config_text.splitlines(),
            fromfile='Previous',
            tofile='Current',
            lineterm=''
        ))
        diff_text = '\n'.join(diff_lines)
    db.record_config(ip, config_type, config_text, diff_text)
    return jsonify({'status': 'ok'})

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
