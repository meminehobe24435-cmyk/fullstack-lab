"""REST API（只用 Python 标准库，无第三方依赖）：分页/筛选/详情/统计 + LRU-TTL 缓存。

接口：
  GET /api/health
  GET /api/devices?page=1&size=20&status=fault&line=A1&order_by=temperature&desc=1
  GET /api/devices/<id>
  GET /api/devices/<id>/readings?limit=10
  GET /api/stats
  GET /api/cache/stats
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                       # noqa: E402
from app.cache import CachedQuery, LruTtlCache   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
DB_PATH = os.environ.get("FS_DB", os.path.join(HERE, "..", "data", "devices.db"))

CACHE = LruTtlCache(capacity=64, ttl_seconds=30.0)
QUERY = CachedQuery(CACHE)
# ThreadingHTTPServer 每个请求跑在新线程里，而 SQLite 连接不能跨线程使用，
# 所以每个线程各持一个连接（线程本地），既安全又不用给每次请求重连。
_LOCAL = threading.local()


def conn():
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        c = db.connect(DB_PATH)
        db.init_schema(c)
        if c.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"] == 0:
            db.seed(c)
        db.create_indexes(c)
        _LOCAL.conn = c
    return c


def close_all():
    c = getattr(_LOCAL, "conn", None)
    if c is not None:
        c.close()
        _LOCAL.conn = None


def _cached(key, loader):
    return QUERY.run(key, loader)


class Handler(BaseHTTPRequestHandler):
    server_version = "fullstack-lab/1.0"

    # ---------- 工具 ----------
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        if not os.path.isfile(path):
            self._json({"error": "not found", "path": path}, 404)
            return
        ctype = "text/html; charset=utf-8"
        if path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):      # 安静一点，测试时好读
        pass

    # ---------- 路由 ----------
    def do_GET(self):                       # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/api/health":
                return self._json({"status": "ok", "db": os.path.basename(DB_PATH)})

            if path == "/api/devices":
                page = int(qs.get("page", ["1"])[0])
                size = int(qs.get("size", ["20"])[0])
                status = qs.get("status", [None])[0]
                line = qs.get("line", [None])[0]
                order_by = qs.get("order_by", ["id"])[0]
                desc = qs.get("desc", ["0"])[0] in ("1", "true", "yes")
                if status and status not in db.STATUSES:
                    return self._json({"error": "invalid status", "allowed": list(db.STATUSES)}, 400)
                if line and line not in db.LINES:
                    return self._json({"error": "invalid line", "allowed": list(db.LINES)}, 400)
                key = "devices:%s:%s:%s:%s:%s:%s" % (page, size, status, line, order_by, desc)
                payload = _cached(key, lambda: db.list_devices(conn(), page, size, status, line, order_by, desc))
                return self._json(payload)

            if path.startswith("/api/devices/"):
                parts = path.strip("/").split("/")      # ['api', 'devices', '<id>', ...]
                if len(parts) < 3 or not parts[2].isdigit():
                    return self._json({"error": "invalid id"}, 400)
                device_id = int(parts[2])
                if len(parts) == 3 and parts[2] == "readings":
                    limit = int(qs.get("limit", ["10"])[0])
                    key = "readings:%d:%d" % (device_id, limit)
                    data = _cached(key, lambda: db.device_readings(conn(), device_id, limit))
                    if data is None:
                        return self._json({"error": "device not found", "id": device_id}, 404)
                    return self._json({"device_id": device_id, "items": data})
                device = _cached("device:%d" % device_id, lambda: db.get_device(conn(), device_id))
                if device is None:
                    return self._json({"error": "device not found", "id": device_id}, 404)
                return self._json(device)

            if path == "/api/stats":
                return self._json(_cached("stats", lambda: db.stats(conn())))

            if path == "/api/cache/stats":
                stats = CACHE.stats()
                stats["db_calls"] = QUERY.db_calls
                return self._json(stats)

            # 静态资源
            if path in ("/", "/index.html"):
                return self._file(os.path.join(STATIC_DIR, "index.html"))
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                if ".." in rel:
                    return self._json({"error": "bad path"}, 400)
                return self._file(os.path.join(STATIC_DIR, rel))
            return self._json({"error": "not found", "path": path}, 404)
        except ValueError as exc:
            return self._json({"error": "bad parameter", "detail": str(exc)}, 400)
        except Exception as exc:                      # 兜底：不要让服务崩掉
            return self._json({"error": "internal error", "detail": str(exc)}, 500)


def make_server(port: int = 8000, db_path: str = None):
    global DB_PATH
    if db_path:
        DB_PATH = db_path
        close_all()
    CACHE.invalidate()
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    port = int(os.environ.get("FS_PORT", "8000"))
    srv = make_server(port)
    print("fullstack-lab 已启动： http://127.0.0.1:%d/" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
