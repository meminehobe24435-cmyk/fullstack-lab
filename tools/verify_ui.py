"""前端验证：起本地服务 → 用 Chrome 无头模式渲染 → 抓 DOM 断言关键内容真的渲染出来了。"""
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.server import make_server                  # noqa: E402

PORT = 8155
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_browser():
    for path in CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def main():
    browser = find_browser()
    if not browser:
        print("✗ 没找到 Chrome/Edge，跳过前端渲染验证")
        return 1

    tmp = tempfile.TemporaryDirectory()
    server = make_server(PORT, os.path.join(tmp.name, "ui.db"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.6)
    url = "http://127.0.0.1:%d/" % PORT

    ok = 0
    fail = 0

    def check(cond, msg):
        nonlocal ok, fail
        if cond:
            ok += 1; print("  PASS  " + msg)
        else:
            fail += 1; print("  FAIL  " + msg)

    try:
        # 1) 后端接口先自证
        with urllib.request.urlopen(url + "api/devices?size=3", timeout=10) as resp:
            body = resp.read().decode("utf-8")
        check('"items"' in body and '"total"' in body, "REST 接口 /api/devices 返回 items+total")

        # 2) 无头渲染，等 JS 跑完（--virtual-time-budget 让 fetch 完成）
        with tempfile.TemporaryDirectory() as prof:
            dom = subprocess.run(
                [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + prof, "--virtual-time-budget=6000",
                 "--dump-dom", url],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=90).stdout or ""

        check("设备数据看板" in dom, "页面标题渲染")
        check("设备总数" in dom and "平均温度" in dom, "统计卡片渲染")
        check('id="device-table"' in dom, "设备表格容器存在")
        check(re.search(r"SN\d{6}", dom) is not None, "表格里渲染出了真实设备序列号（前端-后端联调成功）")
        check("缓存命中率" in dom, "缓存统计渲染")
        check("加载失败" not in dom, "页面没有出现加载错误提示")

        # 3) 截图留证
        shot = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "dashboard.png")
        os.makedirs(os.path.dirname(shot), exist_ok=True)
        with tempfile.TemporaryDirectory() as prof2:
            subprocess.run(
                [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + prof2, "--virtual-time-budget=6000",
                 "--window-size=1280,900", "--screenshot=" + shot, url],
                capture_output=True, timeout=90)
        check(os.path.isfile(shot) and os.path.getsize(shot) > 5000, "已保存整页截图 docs/dashboard.png")
    finally:
        server.shutdown(); server.server_close(); tmp.cleanup()

    print("\n前端验证：%d 项通过，%d 项失败" % (ok, fail))
    print("RESULT: " + ("ALL PASS" if fail == 0 else "FAILED"))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
