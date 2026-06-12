import sqlite3
import os
import time
from datetime import datetime, timedelta
import logging


class HistoryDB:
    """SQLite database layer for SNMP monitoring history and metrics."""

    def __init__(self, db_path='database/history.db'):
        self.db_path = db_path
        self._local = None

    def get_connection(self):
        """Get a new database connection."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        """Create all tables if they don't exist."""
        conn = self.get_connection()
        try:
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS devices (
                ip TEXT PRIMARY KEY,
                hostname TEXT DEFAULT '',
                sys_descr TEXT DEFAULT '',
                sys_uptime TEXT DEFAULT '',
                sys_name TEXT DEFAULT '',
                sys_contact TEXT DEFAULT '',
                sys_location TEXT DEFAULT '',
                last_seen TEXT DEFAULT '',
                status TEXT DEFAULT 'unknown'
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS cpu_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_ip TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                cpu_usage REAL,
                cpu_type TEXT DEFAULT '5min'
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS memory_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_ip TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                pool_name TEXT DEFAULT '',
                used_bytes INTEGER DEFAULT 0,
                free_bytes INTEGER DEFAULT 0
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS interface_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_ip TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                if_index TEXT,
                if_descr TEXT,
                if_alias TEXT DEFAULT '',
                admin_status TEXT DEFAULT '',
                oper_status TEXT DEFAULT '',
                in_octets INTEGER DEFAULT 0,
                out_octets INTEGER DEFAULT 0,
                in_errors INTEGER DEFAULT 0,
                out_errors INTEGER DEFAULT 0,
                speed TEXT DEFAULT '0'
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS config_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_ip TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                config_type TEXT DEFAULT 'running',
                config_text TEXT DEFAULT '',
                diff_from_previous TEXT DEFAULT ''
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_ip TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                alert_type TEXT DEFAULT '',
                severity TEXT DEFAULT 'info',
                message TEXT DEFAULT ''
            )''')

            c.execute('''CREATE TABLE IF NOT EXISTS topology (
                local_ip TEXT NOT NULL,
                local_port TEXT NOT NULL,
                remote_id TEXT NOT NULL,
                remote_port TEXT NOT NULL,
                protocol TEXT DEFAULT 'lldp',
                last_seen TEXT NOT NULL,
                PRIMARY KEY (local_ip, local_port)
            )''')

            # Create indexes for faster history queries
            c.execute('CREATE INDEX IF NOT EXISTS idx_cpu_device_time ON cpu_history(device_ip, timestamp)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_if_device_time ON interface_history(device_ip, timestamp)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_alerts_device_time ON alerts(device_ip, timestamp)')

            conn.commit()
            logging.info("Database initialized successfully")
        except Exception as e:
            logging.error(f"Database init error: {e}")
            conn.rollback()
        finally:
            conn.close()

    def _to_dict(self, row):
        return dict(zip(row.keys(), row))

    def update_device(self, device):
        conn = self.get_connection()
        try:
            conn.execute(
                """INSERT INTO devices (ip, hostname, sys_descr, sys_uptime, sys_name, status, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ip) DO UPDATE SET
                   hostname=excluded.hostname, sys_descr=excluded.sys_descr, 
                   sys_uptime=excluded.sys_uptime, sys_name=excluded.sys_name,
                   status=excluded.status, last_seen=excluded.last_seen""",
                (device['ip'], device.get('hostname'), device.get('sys_descr'), 
                 device.get('sys_uptime'), device.get('sys_name'), device.get('status'),
                 datetime.now().isoformat())
            )
            conn.commit()
        except Exception as e:
            logging.error(f"Error updating device {device['ip']}: {e}")
        finally:
            conn.close()

    def record_neighbor(self, local_ip, local_port, remote_id, remote_port, protocol):
        conn = self.get_connection()
        try:
            conn.execute(
                """INSERT INTO topology (local_ip, local_port, remote_id, remote_port, protocol, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(local_ip, local_port) DO UPDATE SET
                   remote_id=excluded.remote_id, remote_port=excluded.remote_port,
                   protocol=excluded.protocol, last_seen=excluded.last_seen""",
                (local_ip, local_port, remote_id, remote_port, protocol, datetime.now().isoformat())
            )
            conn.commit()
        except Exception as e:
            logging.error(f"Error recording neighbor for {local_ip}: {e}")
        finally:
            conn.close()

    def get_topology(self):
        conn = self.get_connection()
        try:
            rows = conn.execute("SELECT * FROM topology").fetchall()
            return [self._to_dict(row) for row in rows]
        except Exception as e:
            logging.error(f"Error fetching topology: {e}")
            return []
        finally:
            conn.close()

    def get_devices(self):
        conn = self.get_connection()
        try:
            rows = conn.execute("SELECT * FROM devices").fetchall()
            return [self._to_dict(row) for row in rows]
        except Exception as e:
            logging.error(f"Error fetching devices: {e}")
            return []
        finally:
            conn.close()

    def record_cpu(self, device_ip, cpu_usage, cpu_type='5min'):
        conn = self.get_connection()
        try:
            conn.execute(
                "INSERT INTO cpu_history (device_ip, timestamp, cpu_usage, cpu_type) VALUES (?, ?, ?, ?)",
                (device_ip, datetime.now().isoformat(), cpu_usage, cpu_type)
            )
            conn.commit()
        except Exception as e:
            logging.error(f"Error recording CPU for {device_ip}: {e}")
        finally:
            conn.close()

    def record_memory(self, device_ip, pool_name, used_bytes, free_bytes):
        conn = self.get_connection()
        try:
            conn.execute(
                "INSERT INTO memory_history (device_ip, timestamp, pool_name, used_bytes, free_bytes) VALUES (?, ?, ?, ?, ?)",
                (device_ip, datetime.now().isoformat(), pool_name, used_bytes, free_bytes)
            )
            conn.commit()
        except Exception as e:
            logging.error(f"Error recording memory for {device_ip}: {e}")
        finally:
            conn.close()

    def record_interface(self, device_ip, if_index, if_descr, if_alias,
                         admin_status, oper_status,
                         in_octets, out_octets, in_errors, out_errors, speed):
        conn = self.get_connection()
        try:
            conn.execute(
                """INSERT INTO interface_history 
                (device_ip, timestamp, if_index, if_descr, if_alias, admin_status, oper_status,
                 in_octets, out_octets, in_errors, out_errors, speed) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (device_ip, datetime.now().isoformat(), if_index, if_descr, if_alias,
                 admin_status, oper_status, in_octets, out_octets, in_errors, out_errors, speed)
            )
            conn.commit()
        except Exception as e:
            logging.error(f"Error recording interface for {device_ip}: {e}")
        finally:
            conn.close()

    def record_config(self, device_ip, config_type, config_text, diff_from_previous=''):
        conn = self.get_connection()
        try:
            conn.execute(
                "INSERT INTO config_history (device_ip, timestamp, config_type, config_text, diff_from_previous) VALUES (?, ?, ?, ?, ?)",
                (device_ip, datetime.now().isoformat(), config_type, config_text, diff_from_previous)
            )
            conn.commit()
        except Exception as e:
            logging.error(f"Error recording config for {device_ip}: {e}")
        finally:
            conn.close()

    def record_alert(self, device_ip, alert_type, severity, message):
        conn = self.get_connection()
        try:
            conn.execute(
                "INSERT INTO alerts (device_ip, timestamp, alert_type, severity, message) VALUES (?, ?, ?, ?, ?)",
                (device_ip, datetime.now().isoformat(), alert_type, severity, message)
            )
            conn.commit()
        except Exception as e:
            logging.error(f"Error recording alert for {device_ip}: {e}")
        finally:
            conn.close()

    def get_cpu_history(self, device_ip, hours=1):
        conn = self.get_connection()
        try:
            since = (datetime.now() - timedelta(hours=hours)).isoformat()
            rows = conn.execute(
                "SELECT timestamp, cpu_usage FROM cpu_history WHERE device_ip=? AND timestamp>? ORDER BY timestamp",
                (device_ip, since)
            ).fetchall()
            return [[row[0], row[1]] for row in rows]
        except Exception as e:
            logging.error(f"Error fetching CPU history for {device_ip}: {e}")
            return []
        finally:
            conn.close()

    def get_interface_history(self, device_ip, hours=1):
        conn = self.get_connection()
        try:
            since = (datetime.now() - timedelta(hours=hours)).isoformat()
            rows = conn.execute(
                """SELECT timestamp, if_index, if_descr, if_alias, admin_status, oper_status,
                          in_octets, out_octets, in_errors, out_errors, speed
                   FROM interface_history WHERE device_ip=? AND timestamp>? ORDER BY timestamp""",
                (device_ip, since)
            ).fetchall()
            return [list(row) for row in rows]
        except Exception as e:
            logging.error(f"Error fetching interface history for {device_ip}: {e}")
            return []
        finally:
            conn.close()

    def get_config_history(self, device_ip, limit=10, config_type=None):
        conn = self.get_connection()
        try:
            query = "SELECT id, timestamp, config_type, config_text, diff_from_previous FROM config_history WHERE device_ip=?"
            params = [device_ip]
            if config_type:
                query += " AND config_type=?"
                params.append(config_type)
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [self._to_dict(row) for row in rows]
        except Exception as e:
            logging.error(f"Error fetching config history for {device_ip}: {e}")
            return []
        finally:
            conn.close()

    def get_alerts(self, device_ip=None, limit=50):
        conn = self.get_connection()
        try:
            if device_ip:
                rows = conn.execute(
                    "SELECT id, device_ip, timestamp, alert_type, severity, message FROM alerts WHERE device_ip=? ORDER BY timestamp DESC LIMIT ?",
                    (device_ip, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, device_ip, timestamp, alert_type, severity, message FROM alerts ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [self._to_dict(row) for row in rows]
        except Exception as e:
            logging.error(f"Error fetching alerts: {e}")
            return []
        finally:
            conn.close()

    def cleanup_old_records(self, days=30):
        conn = self.get_connection()
        try:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            tables = ['cpu_history', 'memory_history', 'interface_history', 'config_history', 'alerts']
            total_deleted = 0
            for table in tables:
                result = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                total_deleted += result.rowcount
            conn.commit()
            try: conn.execute("VACUUM")
            except: pass
            return total_deleted
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()

    def enforce_size_limit(self, max_mb=20):
        if not os.path.exists(self.db_path): return
        current_size_mb = os.path.getsize(self.db_path) / (1024 * 1024)
        if current_size_mb <= max_mb: return
        conn = self.get_connection()
        try:
            tables = ['cpu_history', 'memory_history', 'interface_history', 'alerts']
            for table in tables:
                conn.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} ORDER BY timestamp ASC LIMIT 5000)")
            conn.commit()
            conn.execute("VACUUM")
        except Exception as e:
            logging.error(f"Error enforcing DB size limit: {e}")
        finally:
            conn.close()
