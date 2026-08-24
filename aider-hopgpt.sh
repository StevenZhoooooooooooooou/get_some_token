#!/usr/bin/env bash
# 用 HopGPT 驱动 aider，在本地 git 仓库里全自动读写文件。
#
# 用法：在你想编辑的项目目录下运行
#     /home/stevenzhou/research/token_steal/aider-hopgpt.sh
# 或先建软链接到 PATH：
#     ln -s /home/stevenzhou/research/token_steal/aider-hopgpt.sh ~/.local/bin/aider-hopgpt
# 然后在任意仓库里直接跑：aider-hopgpt
#
# 常用可选环境变量：
#   HOPGPT_MODEL       主模型（默认 claude-sonnet-4.5，也可 gpt-5.5 / claude-opus / o3）
#   HOPGPT_WEAK_MODEL  弱模型，用于生成提交信息等（默认 hopgpt=o3-mini）
#   AIDER_EDIT_FORMAT  编辑格式（默认 diff；若改动应用失败可改成 whole）
#   PROXY_URL          代理地址（默认 http://127.0.0.1:8787）

set -euo pipefail

PROXY_URL="${PROXY_URL:-http://127.0.0.1:8787}"
AIDER_BIN="${AIDER_BIN:-$HOME/miniforge3/envs/aider/bin/aider}"
MODEL="${HOPGPT_MODEL:-claude-sonnet-4.5}"
WEAK_MODEL="${HOPGPT_WEAK_MODEL:-hopgpt}"
EDIT_FORMAT="${AIDER_EDIT_FORMAT:-diff}"

if [ ! -x "$AIDER_BIN" ]; then
  echo "✗ 找不到 aider（$AIDER_BIN）。请先安装：" >&2
  echo "    ~/miniforge3/envs/aider/bin/uv pip install aider-chat" >&2
  exit 1
fi

# 代理必须在跑，否则 aider 无法调用 HopGPT
if ! curl -sf -m 3 "${PROXY_URL}/health" >/dev/null 2>&1; then
  echo "✗ HopGPT 代理未运行。请先启动：" >&2
  echo "    cd /home/stevenzhou/research/token_steal && ./start.sh" >&2
  exit 1
fi

READY="$(curl -s -m 3 "${PROXY_URL}/health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("ready"))' 2>/dev/null || echo False)"
if [ "$READY" != "True" ]; then
  echo "⚠ 代理在跑但凭证未就绪。请刷新浏览器里的 chat.ai.jh.edu 页面，等扩展推送凭证后重试。" >&2
fi

# litellm 用 openai/ 前缀走 OpenAI 兼容协议，指向本地代理。key 是占位符（代理用浏览器凭证）。
export OPENAI_API_BASE="${PROXY_URL}/v1"
export OPENAI_API_KEY="hopgpt"

exec "$AIDER_BIN" \
  --model "openai/${MODEL}" \
  --weak-model "openai/${WEAK_MODEL}" \
  --edit-format "${EDIT_FORMAT}" \
  --no-show-model-warnings \
  --no-check-update \
  --no-analytics \
  "$@"
