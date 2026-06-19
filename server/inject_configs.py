import os
import sys
import json
import sqlite3
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from database.history_db import HistoryDB

CONFIG_PATH = os.path.join(PROJECT_ROOT, 'config', 'config.json')

def load_config():
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

def generate_cisco_config(ip, hostname, is_startup=False):
    cfg_type = 'startup' if is_startup else 'running'
    lines = [
        f"! Command: show {cfg_type}-config",
        f"! Time: {datetime.now().isoformat()}",
        "!",
        "version 15.2",
        "no service pad",
        "service timestamps debug datetime msec localtime show-timezone",
        "service timestamps log datetime msec localtime show-timezone",
        "service password-encryption",
        "service compress-config",
        "!",
        f"hostname {hostname}",
        "!",
        "boot-start-marker",
        "boot-end-marker",
        "!",
        "logging buffered 65536 debugging",
        "!",
        "aaa new-model",
        "!",
        "aaa authentication login default local group radius",
        "aaa authentication enable default group radius enable",
        "aaa authorization exec default local group radius ",
        "aaa accounting exec default start-stop group radius",
        "aaa accounting commands 15 default start-stop group radius",
        "!",
        "crypto pki trustpoint TP-self-signed-123456",
        " enrollment selfsigned",
        " subject-name cn=IOS-Self-Signed-Certificate",
        " revocation-check none",
        " rsakeypair TP-self-signed-123456",
        "!",
        "spanning-tree mode rapid-pvst",
        "spanning-tree portfast default",
        "spanning-tree portfast bpduguard default",
        "spanning-tree extend system-id",
        "!",
        "vlan internal allocation policy ascending",
        "!",
        "vlan 1",
        " name default",
        "!",
        "vlan 10",
        " name MANAGEMENT",
        "!",
        "vlan 20",
        " name SERVERS",
        "!",
        "vlan 99",
        " name NATIVE",
        "!",
        "lldp run",
        "cdp run",
        "!",
        "class-map match-any AUTOQOS-VOIP-DATA-CLASS",
        " match ip dscp ef",
        "!",
        "policy-map AUTOQOS-SRND4-PM",
        " class AUTOQOS-VOIP-DATA-CLASS",
        "  set dscp ef",
        "  priority percent 33",
        " class class-default",
        "  fair-queue",
        "!"
    ]
    
    # Generate 48 physical interfaces
    for i in range(1, 49):
        lines.append(f"interface GigabitEthernet1/0/{i}")
        if i <= 4:
            lines.append(" description Uplink to Distribution Layer")
            lines.append(" switchport trunk encapsulation dot1q")
            lines.append(" switchport trunk native vlan 99")
            lines.append(" switchport mode trunk")
            lines.append(" load-interval 30")
            lines.append(" carrier-delay msec 0")
        elif i == 48:
            lines.append(" description Out-of-Band Management")
            lines.append(" switchport access vlan 10")
            lines.append(" switchport mode access")
            lines.append(" spanning-tree portfast")
        else:
            lines.append(f" description Server Node {i:02d}")
            lines.append(" switchport access vlan 20")
            lines.append(" switchport mode access")
            lines.append(" spanning-tree portfast")
            lines.append(" storm-control broadcast level 5.00")
            lines.append(" storm-control action trap")
            lines.append(" auto qos trust")
        lines.append("!")
        
    lines.extend([
        "interface TenGigabitEthernet1/1/1",
        " description 10G Core Uplink A",
        " switchport trunk encapsulation dot1q",
        " switchport mode trunk",
        " channel-group 1 mode active",
        "!",
        "interface TenGigabitEthernet1/1/2",
        " description 10G Core Uplink B",
        " switchport trunk encapsulation dot1q",
        " switchport mode trunk",
        " channel-group 1 mode active",
        "!",
        "interface Port-channel1",
        " description LACP 20G to Core",
        " switchport trunk encapsulation dot1q",
        " switchport mode trunk",
        "!",
        "interface Vlan1",
        " no ip address",
        " shutdown",
        "!",
        "interface Vlan10",
        " description Management SVI",
        f" ip address {ip} 255.255.255.0",
        " no ip redirects",
        " no ip unreachables",
        " no ip proxy-arp",
        "!",
        "ip default-gateway 10.160.4.1",
        "ip http server",
        "ip http secure-server",
        "ip ssh version 2",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 3",
        "!",
        "logging trap debugging",
        "logging facility local7",
        "logging source-interface Vlan10",
        "logging host 10.160.4.10",
        "!",
        "snmp-server group READONLY v3 priv read V3READ",
        "snmp-server view V3READ iso included",
        "snmp-server community public RO",
        "snmp-server location Data Center 1",
        "snmp-server contact Network Admin",
        "!",
        "ntp server 10.160.4.10 prefer",
        "ntp server 1.pool.ntp.org",
        "!",
        "line con 0",
        " stopbits 1",
        "line vty 0 4",
        " exec-timeout 15 0",
        " privilege level 15",
        " logging synchronous",
        " transport input ssh",
        "line vty 5 15",
        " exec-timeout 15 0",
        " privilege level 15",
        " logging synchronous",
        " transport input ssh",
        "!",
        "end"
    ])
    
    return "\n".join(lines)

def generate_esxi_config(ip, hostname):
    # Smart "non-default" configuration view for ESXi
    return f"""=== ESXi Non-Default Configuration Settings ===
[System]
Hostname: {hostname}
Management IP: {ip}
NTP Servers: 10.160.4.10, 1.pool.ntp.org
Syslog: udp://10.160.4.10:514

[Network]
vSwitch0: MTU 9000, Promiscuous Mode: ACCEPT
VM Network: VLAN 10
Management Network (vmk0): VLAN 0

[Storage]
Datastore: ds-nvme-01 (Capacity 2TB)

[Security]
SSH Service: Running (Policy: on)
ESXi Shell: Stopped (Policy: off)"""

def generate_vcsa_config(ip, hostname):
    # Smart "non-default" configuration view for VCSA
    return f"""=== VCSA Non-Default Configuration Settings ===
[System]
Hostname: {hostname}
Management IP: {ip}
DNS Servers: 10.160.4.10
NTP Servers: 10.160.4.10

[Services]
vpxd (vCenter Server): RUNNING
vsphere-ui (vSphere Client): RUNNING

[Security]
SSH Service: Running
Appliance Shell: Enabled"""

def run():
    config = load_config()
    db_path = config['history']['db_path']
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)
    
    db = HistoryDB(db_path)
    
    devices = config.get('monitored_switches', [])
    for dev in devices:
        ip = dev.get('ip')
        hostname = dev.get('hostname') or ip
        target_type = dev.get('target_type', 'cisco').lower()
        
        # Inject Running Config
        running_cfg = ""
        startup_cfg = ""
        
        if target_type == 'cisco' or target_type == 'switch':
            running_cfg = generate_cisco_config(ip, hostname, is_startup=False)
            startup_cfg = generate_cisco_config(ip, hostname, is_startup=True)
        elif target_type == 'esxi':
            running_cfg = generate_esxi_config(ip, hostname)
            startup_cfg = running_cfg # ESXi doesn't have a distinct "startup" text format in our smart view
        elif target_type == 'vcsa':
            running_cfg = generate_vcsa_config(ip, hostname)
            startup_cfg = running_cfg
        else:
            running_cfg = f"Configuration extraction for {target_type} is unsupported."
            startup_cfg = running_cfg

        # Clear existing to avoid duplicates if run multiple times
        # db.record_config inserts a new record. We will just insert new ones at the top of time.
        db.record_config(ip, 'running', running_cfg, diff_from_previous='')
        time.sleep(1) # Ensure timestamp order
        db.record_config(ip, 'startup', startup_cfg, diff_from_previous='')

    print("Config injection complete.")

if __name__ == '__main__':
    run()
