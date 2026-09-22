"""自检：python -m unittest discover -s tests -v （只用标准库）"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                  # noqa: E402
from app.cache import CachedQuery, LruTtlCache      # noqa: E402
from app.server import make_server                  # noqa: E402

PORT = 8137


class CacheTest(unittest.TestCase):
    def test_hit_and_miss_stats(self):
        cache = LruTtlCache(capacity=2, ttl_seconds=60)
        self.assertIsNone(cache.get("a"))
        cache.set("a", 1)
        self.assertEqual(cache.get("a"), 1)
        st = cache.stats()
        self.assertEqual((st["hits"], st["misses"]), (1, 1))
        self.assertAlmostEqual(st["hit_rate"], 0.5)

    def test_lru_eviction(self):
        cache = LruTtlCache(capacity=2, ttl_seconds=60)
        cache.set("a", 1); cache.set("b", 2)
        cache.get("a")                    # a 变最近使用
        cache.set("c", 3)                 # 应淘汰 b
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.stats()["evictions"], 1)

    def test_ttl_expiration(self):
        cache = LruTtlCache(capacity=4, ttl_seconds=-1)   # 立刻过期
        cache.set("k", 1)
        self.assertIsNone(cache.get("k"))
        self.assertEqual(cache.stats()["expirations"], 1)

    def test_cached_query_calls_loader_once(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return {"v": 42}

        cq = CachedQuery(LruTtlCache(capacity=4, ttl_seconds=60))
        self.assertEqual(cq.run("k", loader), {"v": 42})
        self.assertEqual(cq.run("k", loader), {"v": 42})
        self.assertEqual(calls["n"], 1)
        self.assertEqual(cq.db_calls, 1)


class DbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.conn = db.connect(os.path.join(cls.tmp.name, "t.db"))
        db.init_schema(cls.conn)
        db.seed(cls.conn, devices=60, readings_per_device=5)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close(); cls.tmp.cleanup()

    def test_paging_and_total(self):
        page1 = db.list_devices(self.conn, page=1, size=10)
        page2 = db.list_devices(self.conn, page=2, size=10)
        self.assertEqual(page1["total"], 60)
        self.assertEqual(len(page1["items"]), 10)
        self.assertNotEqual(page1["items"][0]["id"], page2["items"][0]["id"])

    def test_filter_and_order(self):
        rows = db.list_devices(self.conn, status="fault", order_by="temperature", desc=True)["items"]
        self.assertTrue(all(r["status"] == "fault" for r in rows))
        temps = [r["temperature"] for r in rows]
        self.assertEqual(temps, sorted(temps, reverse=True))

    def test_readings_desc_order(self):
        rows = db.device_readings(self.conn, 1, limit=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual([r["ts"] for r in rows], sorted([r["ts"] for r in rows], reverse=True))

    def test_index_changes_query_plan_and_speed(self):
        sql = "SELECT ts, voltage, current FROM readings WHERE device_id = ? ORDER BY ts DESC LIMIT 10"
        db.drop_indexes(self.conn)
        plan_before = db.query_plan(self.conn, sql, (7,))
        time_before = db.time_query(self.conn, sql, (7,), repeat=50)
        db.create_indexes(self.conn)
        plan_after = db.query_plan(self.conn, sql, (7,))
        time_after = db.time_query(self.conn, sql, (7,), repeat=50)

        self.assertIn("SCAN", plan_before.upper())          # 没索引只能全表扫描
        self.assertIn("INDEX", plan_after.upper())          # 加索引后走索引
        self.assertLess(time_after, time_before)            # 且更快
        print("\n    [索引实测] %s → %s" % (plan_before, plan_after))
        print("    [索引实测] %.4f ms → %.4f ms（提速 %.1f 倍）"
              % (time_before, time_after, time_before / max(time_after, 1e-9)))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = make_server(PORT, os.path.join(cls.tmp.name, "api.db"))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % PORT

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        try:
            cls.tmp.cleanup()               # Windows 上可能仍被占用，忽略即可
        except Exception:
            pass

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health(self):
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_devices_paging_and_filter(self):
        status, body = self.get("/api/devices?page=1&size=5")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 5)
        self.assertGreater(body["total"], 100)
        status, body = self.get("/api/devices?status=fault&size=100")
        self.assertTrue(all(d["status"] == "fault" for d in body["items"]))

    def test_bad_parameter(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/devices?status=nope")
        self.assertEqual(ctx.exception.code, 400)

    def test_detail_and_404(self):
        status, body = self.get("/api/devices?size=1")
        device_id = body["items"][0]["id"]
        status, detail = self.get("/api/devices/%d" % device_id)
        self.assertEqual(detail["id"], device_id)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/devices/999999")
        self.assertEqual(ctx.exception.code, 404)

    def test_cache_actually_helps(self):
        self.get("/api/stats")
        _, before = self.get("/api/cache/stats")
        for _ in range(5):
            self.get("/api/stats")
        _, after = self.get("/api/cache/stats")
        self.assertGreaterEqual(after["hits"] - before["hits"], 4)
        self.assertGreater(after["hit_rate"], 0.0)

    def test_static_page_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("设备数据看板", html)
        self.assertIn("/static/app.js", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
