"""SQLite 数据层：建表、造数据、分页/筛选查询，以及**索引优化实测**（EXPLAIN QUERY PLAN + 计时）。

索引优化的证据不是"我加了索引"，而是：同样的查询，加索引前后
① 查询计划从 SCAN 变成 SEARCH ... USING INDEX；② 耗时下降到原来的几分之一。
"""
from __future__ import annotations

import os
import random
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY,
    sn          TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    line        TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    temperature REAL    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL,
    ts          TEXT    NOT NULL,
    voltage     REAL    NOT NULL,
    current     REAL    NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id)
);
"""

STATUSES = ("running", "idle", "fault", "maintenance")
MODELS = ("SX-100", "SX-200", "SX-300")
LINES = ("A1", "A2", "B1", "B2")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def seed(conn: sqlite3.Connection, devices: int = 400, readings_per_device: int = 25,
         seed_value: int = 20260922) -> Tuple[int, int]:
    """造可复现的数据（固定随机种子）。"""
    rng = random.Random(seed_value)
    conn.execute("DELETE FROM readings")
    conn.execute("DELETE FROM devices")
    now = time.time()
    for d in range(1, devices + 1):
        created = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - rng.randint(0, 86400 * 30)))
        conn.execute(
            "INSERT INTO devices (id, sn, model, line, status, temperature, created_at) VALUES (?,?,?,?,?,?,?)",
            (d, "SN%06d" % d, rng.choice(MODELS), rng.choice(LINES), rng.choice(STATUSES),
             round(rng.uniform(20.0, 75.0), 2), created),
        )
        for r in range(readings_per_device):
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - (readings_per_device - r) * 60))
            conn.execute(
                "INSERT INTO readings (device_id, ts, voltage, current) VALUES (?,?,?,?)",
                (d, ts, round(rng.uniform(3.2, 3.6), 4), round(rng.uniform(0.05, 2.5), 4)),
            )
    conn.commit()
    return devices, devices * readings_per_device


def drop_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_readings_device_ts")
    conn.execute("DROP INDEX IF EXISTS idx_devices_status_line")
    conn.commit()


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings(device_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_devices_status_line ON devices(status, line);
    """)
    conn.commit()


def query_plan(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(r["detail"]) for r in rows)


def time_query(conn: sqlite3.Connection, sql: str, params: Tuple = (), repeat: int = 200) -> float:
    """跑 repeat 次取平均耗时（毫秒）。"""
    start = time.perf_counter()
    for _ in range(repeat):
        conn.execute(sql, params).fetchall()
    return (time.perf_counter() - start) * 1000.0 / repeat


def list_devices(conn: sqlite3.Connection, page: int = 1, size: int = 20,
                 status: Optional[str] = None, line: Optional[str] = None,
                 order_by: str = "id", desc: bool = False) -> Dict:
    """分页 + 筛选 + 排序。order_by 用白名单，避免拼接注入。"""
    allowed = {"id": "id", "sn": "sn", "temperature": "temperature", "created_at": "created_at"}
    column = allowed.get(order_by, "id")
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if line:
        where.append("line = ?")
        params.append(line)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute("SELECT COUNT(*) AS c FROM devices" + clause, params).fetchone()["c"]
    page = max(1, int(page))
    size = max(1, min(200, int(size)))
    sql = ("SELECT id, sn, model, line, status, temperature, created_at FROM devices" + clause +
           " ORDER BY %s %s LIMIT ? OFFSET ?" % (column, "DESC" if desc else "ASC"))
    rows = conn.execute(sql, params + [size, (page - 1) * size]).fetchall()
    return {"total": total, "page": page, "size": size,
            "items": [dict(r) for r in rows]}


def get_device(conn: sqlite3.Connection, device_id: int) -> Optional[Dict]:
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def device_readings(conn: sqlite3.Connection, device_id: int, limit: int = 20) -> List[Dict]:
    """按设备取最近 N 条读数 —— 这是最吃索引的查询（device_id + ts DESC）。"""
    rows = conn.execute(
        "SELECT ts, voltage, current FROM readings WHERE device_id = ? ORDER BY ts DESC LIMIT ?",
        (device_id, limit)).fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> Dict:
    total = conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
    by_status = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) AS c FROM devices GROUP BY status").fetchall()}
    agg = conn.execute("SELECT AVG(temperature) AS t, MAX(temperature) AS m FROM devices").fetchone()
    faults = conn.execute("SELECT COUNT(*) AS c FROM devices WHERE status = 'fault'").fetchone()["c"]
    return {"devices": total, "by_status": by_status,
            "avg_temperature": round(agg["t"] or 0.0, 2),
            "max_temperature": round(agg["m"] or 0.0, 2),
            "fault_ratio": round(faults / total, 4) if total else 0.0}
