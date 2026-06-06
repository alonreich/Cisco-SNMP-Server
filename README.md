# Cisco SNMP Server Monitoring Dashboard

A high-performance, real-time monitoring solution for Cisco network infrastructure. This dashboard provides live visibility into interface bandwidth utilization and device CPU load via SNMPv2c.

## Key Features

- **Dual Monitoring Views**: 
    - **Bandwidth Dashboard**: Sorted leaderboard of active interfaces by utilization.
    - **CPU Dashboard**: Real-time compute load monitoring for all switches.
- **Intelligent Filtering**: 
    - Automatically hides administrative/operationally down ports.
    - Filters out idle interfaces (bandwidth < 0.5 Mbps) to reduce noise.
- **Hardware Health Alerts**: 
    - Detects and highlights "Link Speed Degradation" (e.g., a Gigabit port running at 100Mbps).
    - Provides interactive tooltips with troubleshooting advice.
- **Optimized UI**: 
    - Interface name shortening (e.g., `GigabitEthernet` -> `Gi`) for maximum data density.
    - Modern, responsive Dark Mode interface built with Tailwind CSS.
- **Dynamic Configuration**: 
    - Manage switches and polling intervals (5s to 1h) directly from the browser.
    - Persistent settings saved to `config.json`.
- **Zero-Cache Architecture**: 
    - Strictly enforced policy against `__pycache__` and bytecode generation.
- **Background Persistence**:
    - Automatically registers as a Windows Scheduled Task for silent, background operation at boot.

## Quick Start (Windows)

1. **Install**: Run `install.bat` (as Administrator) to create the virtual environment, install dependencies, and register the background task.
2. **Configure**: Copy `config.json.example` to `config.json` (optional, can be done via UI).
3. **Launch**: The monitor starts automatically after `install.bat` and on every system boot.
4. **Uninstall**: Run `uninstall.bat` (as Administrator) to completely remove the project.
5. **Access**: Open `http://localhost:8000` in your browser.

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, Uvicorn, PySNMP.
- **Frontend**: Vanilla ES6+, HTML5, Tailwind CSS.
- **Protocol**: SNMPv2c (UDP/161), WebSockets.

## Feature Logic & Thresholds

- **Bandwidth Legend**: 
  - <span style="color:#22c55e">●</span> < 30% (Normal)
  - <span style="color:#eab308">●</span> 30-75% (Warning)
  - <span style="color:#ef4444">●</span> > 75% (Critical)
- **CPU Legend**: 
  - <span style="color:#22c55e">●</span> 0-25% (Healthy)
  - <span style="color:#eab308">●</span> 26-65% (Moderate)
  - <span style="color:#ef4444">●</span> 66-100% (High Load)

## License

Internal use — Full Control project.
