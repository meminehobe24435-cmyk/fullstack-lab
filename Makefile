# fullstack-lab —— 后端 / 前端 / 数据库 / 缓存 一体化验证
PY      ?= python
CHROME  ?= C:/Program Files/Google/Chrome/Application/chrome.exe
PORT    ?= 8000

.PHONY: seed serve test bench verify-ui clean
seed:
	@$(PY) tools/bench.py --seed-only

serve:
	@$(PY) -m app.server

test:
	@$(PY) -m unittest discover -s tests -v

bench:
	@$(PY) tools/bench.py

# 前端渲染验证：起服务 → Chrome 无头抓 DOM → 断言关键元素与数据
verify-ui:
	@$(PY) tools/verify_ui.py

clean:
	-@cmd /c rmdir /s /q data 2>nul
