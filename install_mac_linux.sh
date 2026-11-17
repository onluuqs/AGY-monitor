#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/ag-monitor/server.py" ]; then
  SRC_PLUGIN="${SCRIPT_DIR}/ag-monitor"
else
  SRC_PLUGIN="${SCRIPT_DIR}"
fi
DEST_DIR="${HOME}/.gemini/config/plugins/ag-monitor"

PY_BIN="$(command -v python3 || command -v python || true)"
if [ -z "${PY_BIN}" ]; then
  echo "[错误] 未检测到 python3，请先安装 Python 3.9+。"
  exit 1
fi

echo "[1/3] 检测到 Python: ${PY_BIN}"
echo "[2/3] 正在安装全局插件至: ${DEST_DIR}"
mkdir -p "${DEST_DIR}"
for f in plugin.json mcp_config.json server.py index.html run_mcp.cmd start_monitor.bat logo.svg README.md; do
  if [ -f "${SRC_PLUGIN}/${f}" ]; then
    cp "${SRC_PLUGIN}/${f}" "${DEST_DIR}/${f}"
  fi
done

"${PY_BIN}" - "${PY_BIN}" "${DEST_DIR}" <<'PYEOF'
import json, os, sys
py, dst = sys.argv[1], sys.argv[2]
cfg = {
    "mcpServers": {
        "ag-monitor-daemon": {
            "command": py,
            "args": [os.path.join(dst, "server.py"), "--mcp", "--host", "127.0.0.1", "--port", "8765"],
        }
    }
}
with open(os.path.join(dst, "mcp_config.json"), "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("
")
PYEOF

echo "[3/3] 正在启动后台监控服务 (http://127.0.0.1:8765) ..."
nohup "${PY_BIN}" "${DEST_DIR}/server.py" --host 127.0.0.1 --port 8765 >/dev/null 2>&1 &
sleep 1
if command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:8765"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://127.0.0.1:8765" >/dev/null 2>&1 || true
fi
echo "[完成] 以后打开 Antigravity 将自动同步启动监控台：http://127.0.0.1:8765"
