# Network Bandwidth Monitor

Real-time Cisco switch interface utilization dashboard for Windows Server. Polls interface counters via SNMPv2c, computes delta bandwidth metrics, and streams a sorted leaderboard over WebSockets.

## Requirements

- Windows Server 2016+ or Windows 10/11
- Python 3.11 or newer
- Network access to target Cisco switches on UDP port 161 (SNMP)

## Quick Start (Command Prompt — recommended)

```cmd
cd C:\SNMP-Server
setup.bat
master.bat
```

`setup.bat` creates `.\venv` with your local Python and installs **all** packages from `requirements.txt` (FastAPI, Uvicorn, Jinja2, PySNMP).

Open **http://localhost:8000** in a browser.

> **Important:** Do not install packages globally with `pip install uvicorn` alone.  
> Always use `setup.bat` so `master.bat` runs the correct isolated environment.

### If `master.bat` says Python not found

Your `venv` was built on another PC or for Python 3.13. Run `setup.bat` again — it recreates `venv` using `py -3.14` / `py -3` on this machine.

### Manual setup (PowerShell)

```powershell
cd C:\SNMP-Server
py -3.14 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python master.py
```

## Production Deployment (Windows Service style)

Run with Uvicorn bound to all interfaces:

```powershell
.\venv\Scripts\Activate.ps1
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

> **Note:** Use a single worker (`--workers 1`) so the in-memory `GLOBAL_STATE` and polling loop remain consistent.

### Windows Firewall

Allow inbound TCP on port 8000 if remote browsers need access:

```powershell
New-NetFirewallRule -DisplayName "Bandwidth Monitor" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

## Configuration

All settings are editable from the dashboard UI or via the REST API.

| Variable | Description | Default | Options |
|---|---|---|---|
| `MONITORED_SWITCHES` | Up to 5 switch IPv4 addresses | 5 empty slots | Any valid IPv4 |
| `SNMP_COMMUNITY` | SNMPv2c read community | `public` | Any string |
| `POLLING_INTERVAL` | Seconds between SNMP poll cycles | `10` | 5, 10, 30, 60 |
| `DISPLAY_LIMIT` | Top-N interfaces shown (by utilization %) | `10` | 10, 20, 30 |

### REST API

```powershell
# Read current config
Invoke-RestMethod -Uri http://localhost:8000/api/config

# Update config
$body = @{
    monitored_switches = @("10.0.0.1", "10.0.0.2", "", "", "")
    snmp_community     = "mycommunity"
    polling_interval   = 30
    display_limit      = 20
} | ConvertTo-Json

Invoke-RestMethod -Uri http://localhost:8000/api/config -Method POST -Body $body -ContentType "application/json"
```

## Architecture

```
┌─────────────┐     WebSocket (/ws)      ┌──────────────────────────┐
│   Browser   │ ◄──────────────────────► │  FastAPI (main.py)       │
│  Dashboard  │     REST (/api/config)   │                          │
└─────────────┘                          │  ┌────────────────────┐  │
                                         │  │ Polling Worker     │  │
                                         │  │ (asyncio loop)     │  │
                                         │  └────────┬───────────┘  │
                                         │           │ SNMPv2c       │
                                         │  ┌────────▼───────────┐  │
                                         │  │ GLOBAL_STATE       │  │
                                         │  │ (asyncio.Lock)     │  │
                                         │  └────────────────────┘  │
                                         └──────────┬───────────────┘
                                                    │ UDP/161
                                         ┌──────────▼───────────────┐
                                         │  Cisco Switches (x5)     │
                                         └──────────────────────────┘
```

### SNMP OIDs Polled

| OID | Name | Purpose |
|---|---|---|
| `1.3.6.1.2.1.2.2.1.2` | ifDescr | Interface name |
| `1.3.6.1.2.1.31.1.1.1.15` | ifHighSpeed | Link speed (Mbps) |
| `1.3.6.1.2.1.31.1.1.1.6` | ifHCInOctets | 64-bit inbound counter |
| `1.3.6.1.2.1.31.1.1.1.10` | ifHCOutOctets | 64-bit outbound counter |

### Bandwidth Calculation

```
Current_Mbps     = ((ΔIn + ΔOut) × 8) / (ΔTime × 1,000,000)
Current_Util_Pct = (Current_Mbps / ifHighSpeed) × 100
```

Edge cases handled:
- **Counter rollover:** delta skipped when current octets < previous octets
- **Zero speed:** utilization forced to 0% when `ifHighSpeed == 0`

## Project Structure

```
SNMP-Server/
├── master.py            # **Run this** — starts SNMP polling + web server
├── master.bat           # Windows double-click launcher for master.py
├── main.py              # FastAPI app, SNMP worker, WebSocket server
├── requirements.txt     # Python dependencies
├── README.md            # This file
└── templates/
    └── index.html       # Dashboard UI (Tailwind + vanilla JS)
```

### Utilization bar colors

| Utilization | Color |
|---|---|
| Less than 30% | Green |
| 30% to 75% | Yellow |
| Greater than 75% | Red |

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| No data in table | Switch unreachable or wrong community | Verify SNMP access: `snmpwalk -v2c -c public <IP> 1.3.6.1.2.1.2.2.1.2` |
| WebSocket shows Disconnected | Server not running or firewall blocking | Run `python master.py`; allow port 8000 |
| All utilization at 0% | First poll cycle (no delta yet) | Wait one full polling interval |
| Too many rows | Display limit set to Top 30 | Use Top 10 or Top 20 in the dashboard |

## License

Internal use — Full Control project.
