"""索引优化与缓存收益实测：输出可复现的数字（这是"优化"二字的凭据）。"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                  # noqa: E402
from app.cache import CachedQuery, LruTtlCache      # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "devices.db")

SQL = "SELECT ts, voltage, current FROM readings WHERE device_id = ? ORDER BY ts DESC LIMIT 10"


def main():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = db.connect(DB)
    db.init_schema(conn)

    if "--seed-only" in sys.argv or conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"] == 0:
        devices, readings = db.seed(conn)
        print("已生成数据：%d 台设备 / %d 条读数" % (devices, readings))

    print("\n=== ① 索引优化实测（同一查询，加索引前后）===")
    db.drop_indexes(conn)
    plan_before = db.query_plan(conn, SQL, (7,))
    t_before = db.time_query(conn, SQL, (7,), repeat=500)
    db.create_indexes(conn)
    plan_after = db.query_plan(conn, SQL, (7,))
    t_after = db.time_query(conn, SQL, (7,), repeat=500)
    print("  加索引前：%s" % plan_before)
    print("  加索引后：%s" % plan_after)
    print("  平均耗时：%.4f ms → %.4f ms，提速 %.1f 倍" % (t_before, t_after, t_before / max(t_after, 1e-9)))

    print("\n=== ② 分页/筛选查询计划 ===")
    print("  status 筛选：%s" % db.query_plan(
        conn, "SELECT id FROM devices WHERE status = ? AND line = ?", ("fault", "A1")))

    print("\n=== ③ LRU/TTL 缓存收益（模拟 1000 次带热点分布的请求）===")
    cache = LruTtlCache(capacity=32, ttl_seconds=60)
    cq = CachedQuery(cache)
    import random
    rng = random.Random(7)
    hot = [1, 2, 3, 4, 5]                       # 20% 的热点
    for _ in range(1000):
        device_id = rng.choice(hot) if rng.random() < 0.8 else rng.randint(1, 200)
        cq.run("readings:%d" % device_id, lambda i=device_id: db.device_readings(conn, i, 10))
    st = cache.stats()
    print("  请求 1000 次：命中 %d，未命中 %d，实际查库 %d 次，命中率 %.1f%%"
          % (st["hits"], st["misses"], cq.db_calls, st["hit_rate"] * 100))
    print("  淘汰 %d 次，过期 %d 次" % (st["evictions"], st["expirations"]))

    print("\n=== ④ 统计接口 ===")
    print("  %s" % db.stats(conn))
    conn.close()


if __name__ == "__main__":
    main()
