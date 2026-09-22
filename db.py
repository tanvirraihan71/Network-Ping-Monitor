"""
db.py
=====
SQLite storage layer for the Network Ping Monitor.

Stores:
  - ping_history   : every single ping result ever recorded (raw data)
  - outages        : up/down transition log per host, with duration
  - host_settings  : per-host overrides (count/timeout/interval, custom
                      up/down action commands, custom quality thresholds)

All calls are protected by a lock since pings are recorded from a
background thread while the GUI thread may read at the same time.
SQLite's own file-level locking would serialize these anyway, but the
explicit lock keeps behavior predictable and avoids "database is locked"
errors under load.
"""

import json
import sqlite3
import threading
from datetime import datetime

_lock = threading.Lock()
_conn = None
_db_path = "ping_monitor_history.db"


def init_db(path="ping_monitor_history.db"):
    global _conn, _db_path
    _db_path = path
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL;")
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ping_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                hostname TEXT,
                ts TEXT NOT NULL,
                status TEXT NOT NULL,
                rtt_ms REAL,
                loss_pct REAL,
                quality TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ping_history_ip_ts
                ON ping_history(ip, ts);

            CREATE TABLE IF NOT EXISTS outages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                hostname TEXT,
                down_at TEXT NOT NULL,
                up_at TEXT,
                duration_sec REAL
            );
            CREATE INDEX IF NOT EXISTS idx_outages_ip ON outages(ip);

            CREATE TABLE IF NOT EXISTS host_settings (
                ip TEXT PRIMARY KEY,
                settings_json TEXT NOT NULL
            );
            """
        )
        _conn.commit()
    return _conn


def _ensure():
    if _conn is None:
        init_db(_db_path)


def record_ping(ip, hostname, status, rtt_ms, loss_pct, quality, ts=None):
    _ensure()
    ts = ts or datetime.now().isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "INSERT INTO ping_history (ip, hostname, ts, status, rtt_ms, loss_pct, quality) "
            "VALUES (?,?,?,?,?,?,?)",
            (ip, hostname, ts, status, rtt_ms, loss_pct, quality),
        )
        _conn.commit()


def get_history(ip, start=None, end=None, limit=2000):
    _ensure()
    q = "SELECT ts, status, rtt_ms, loss_pct, quality FROM ping_history WHERE ip=?"
    params = [ip]
    if start:
        q += " AND ts >= ?"
        params.append(start)
    if end:
        q += " AND ts <= ?"
        params.append(end)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with _lock:
        cur = _conn.execute(q, params)
        return cur.fetchall()


def open_outage(ip, hostname, ts=None):
    _ensure()
    ts = ts or datetime.now().isoformat(timespec="seconds")
    with _lock:
        cur = _conn.execute("SELECT id FROM outages WHERE ip=? AND up_at IS NULL", (ip,))
        if cur.fetchone():
            return
        _conn.execute(
            "INSERT INTO outages (ip, hostname, down_at) VALUES (?,?,?)", (ip, hostname, ts)
        )
        _conn.commit()


def close_outage(ip, ts=None):
    _ensure()
    ts = ts or datetime.now().isoformat(timespec="seconds")
    with _lock:
        cur = _conn.execute(
            "SELECT id, down_at FROM outages WHERE ip=? AND up_at IS NULL ORDER BY id DESC LIMIT 1",
            (ip,),
        )
        row = cur.fetchone()
        if not row:
            return
        outage_id, down_at = row
        duration = None
        try:
            down_dt = datetime.fromisoformat(down_at)
            up_dt = datetime.fromisoformat(ts)
            duration = (up_dt - down_dt).total_seconds()
        except Exception:
            pass
        _conn.execute(
            "UPDATE outages SET up_at=?, duration_sec=? WHERE id=?", (ts, duration, outage_id)
        )
        _conn.commit()


def get_outages(ip, limit=500):
    _ensure()
    with _lock:
        cur = _conn.execute(
            "SELECT down_at, up_at, duration_sec FROM outages WHERE ip=? "
            "ORDER BY down_at DESC LIMIT ?",
            (ip, limit),
        )
        return cur.fetchall()


def get_host_settings(ip):
    _ensure()
    with _lock:
        cur = _conn.execute("SELECT settings_json FROM host_settings WHERE ip=?", (ip,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else {}


def save_host_settings(ip, settings: dict):
    _ensure()
    payload = json.dumps(settings)
    with _lock:
        _conn.execute(
            "INSERT INTO host_settings (ip, settings_json) VALUES (?, ?) "
            "ON CONFLICT(ip) DO UPDATE SET settings_json=excluded.settings_json",
            (ip, payload),
        )
        _conn.commit()


def get_all_host_settings():
    _ensure()
    with _lock:
        cur = _conn.execute("SELECT ip, settings_json FROM host_settings")
        return {ip: json.loads(js) for ip, js in cur.fetchall()}
