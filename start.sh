#!/usr/bin/env bash
# 一键启动 HopGPT 代理。Cursor 同机使用时不需要 cloudflared。
set -e
cd "$(dirname "$0")"

PORT=8787
URL="http://127.0.0.1:${PORT}"
WAIT_SECS="${WAIT_SECS:-90}"

# 依赖
python3 -c "import fastapi, curl_cffi" 2>/dev/null || pip install -r requirements.txt -q

# 已在跑就跳过
if curl -sf "${URL}/health" >/dev/null 2>&1; then
  echo "✓ 代理已在运行 ${URL}"
else
  echo "启动代理..."
  nohup python3 proxy.py > proxy.log 2>&1 &
  for i in $(seq 1 15); do
    curl -sf "${URL}/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

# 等待凭证就绪（轮询，最多 WAIT_SECS 秒）
python3 - <<PY
import json, time, urllib.request, sys

url = "http://127.0.0.1:8787/health"
wait_secs = int("${WAIT_SECS}")

def fetch():
    return json.load(urllib.request.urlopen(url, timeout=3))

h = fetch()
if not h.get("ready"):
    print()
    print("等待浏览器推送凭证（最多 {} 秒）...".format(wait_secs))
    print("→ 保持 https://chat.ai.jh.edu 标签页开着，浏览器扩展需已启用")
    print()
    for i in range(wait_secs):
        time.sleep(1)
        h = fetch()
        if h.get("ready"):
            print("✓ 凭证已就绪（等待了 {} 秒）".format(i + 1))
            break
        if (i + 1) % 10 == 0:
            print("  ...仍在等待（{}s）".format(i + 1))

ready = h.get("ready")
exp = h.get("token_expires_in", 0)
print()
print("════════════════════════════════════════")
print("  代理地址:  http://127.0.0.1:8787/v1")
print("  默认模型:  hopgpt")
print("  凭证状态:  {}".format("✓ 就绪" if ready else "✗ 未就绪"))
if exp:
    print("  token 剩余: {} 分钟".format(exp // 60))
if h.get("hint"):
    print("  ⚠ {}".format(h["hint"]))
print("════════════════════════════════════════")
print()
if not ready:
    print("→ 在浏览器打开 https://chat.ai.jh.edu 并刷新")
    print("→ 确认 chrome://extensions 里 HopGPT Credential Keeper 已启用")
    print("→ 发一条消息后等 10 秒再运行 ./start.sh")
    sys.exit(1)
else:
    print("→ VS Code / Cursor 聊天框选 HopGPT 模型即可使用")
PY
