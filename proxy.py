"""
HopGPT (LibreChat) -> OpenAI/Anthropic-compatible proxy, curl_cffi edition.

curl_cffi impersonates Chrome's TLS fingerprint, which is required to pass the
Cloudflare bot check in front of chat.ai.jh.edu. Credentials (Bearer token +
cookies) are re-read from disk on every request so your browser (via the
browser extension) can rotate them underneath us.

Endpoints:
    GET  /v1/models              (OpenAI list)
    POST /v1/chat/completions    (OpenAI Chat Completions API)
    POST /v1/messages            (Anthropic Messages API)
    GET  /health

LibreChat 0.8.5 flow:
    POST /api/agents/chat/{endpoint}          -> {"streamId": ...}
    GET  /api/agents/chat/stream/{streamId}   -> Server-Sent Events
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid

from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("hopgpt")

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, ".token")
COOKIE_FILE = os.path.join(HERE, ".cookies")
UA_FILE = os.path.join(HERE, ".ua")

BASE_URL = "https://chat.ai.jh.edu"
# Fallback UA; overridden by the browser's real UA (pushed via /__capture) so
# that Cloudflare's cf_clearance cookie — which is bound to the exact UA —
# validates against our curl_cffi requests.
DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"

# Model map: client-facing name -> (endpoint, upstream model id).
# Only two endpoints currently have working credentials:
#   - AzureOpenAI  (school's paid Azure deployment)
#   - AnthropicClaude (school's Claude deployment)
# openAI / azureOpenAI return 429 "no credits"; anthropic / google have no key.
MODELS = {
    "hopgpt": ("AzureOpenAI", "o3-mini"),  # 默认别名，Cursor 里只选这一个即可
    "o3": ("AzureOpenAI", "o3"),
    "o3-mini": ("AzureOpenAI", "o3-mini"),
    "gpt-5.4": ("AzureOpenAI", "gpt-5.4"),
    "gpt-5.4-mini": ("AzureOpenAI", "gpt-5.4-mini"),
    "gpt-5.4-nano": ("AzureOpenAI", "gpt-5.4-nano"),
    "gpt-5.5": ("AzureOpenAI", "gpt-5.5"),
    "claude-sonnet": ("AnthropicClaude", "claude-sonnet-5-cached"),
    "claude-sonnet-4.5": ("AnthropicClaude", "claude-sonnet-4.5-cached"),
    "claude-opus": ("AnthropicClaude", "claude-opus-4.5-cached"),
    "claude-haiku": ("AnthropicClaude", "claude-haiku-4.5"),
}

DEFAULT_MODEL = "hopgpt"

# Cursor 可能发送的内置模型名 → 映射到可用模型
MODEL_ALIASES = {
    "gpt-4o": "o3-mini",
    "gpt-4o-mini": "o3-mini",
    "gpt-4": "o3-mini",
    "gpt-4.1": "gpt-5.4",
    "gpt-4.1-mini": "gpt-5.4-mini",
    "gpt-5": "gpt-5.4",
    "gpt-5-mini": "gpt-5.4-mini",
    "composer": "o3-mini",
    "auto": "o3-mini",
}

# LibreChat requires an `endpointType` for renamed/custom endpoints; without it
# the server can't find a schema and replies "Error parsing conversation".
ENDPOINT_TYPES = {
    "AzureOpenAI": "azureOpenAI",
    "AnthropicClaude": "anthropic",
}

# 格式引导：让 HopGPT 在回答「创建/修改代码」时，输出 VS Code 能识别并
# 提供「Apply in Editor」按钮的格式 —— 即 `### 相对路径` 标题 + 紧随的代码块。
# VS Code Copilot Chat 的 Ask 模式会解析 `### 路径` 标题，给后面的代码块打上
# codeblockUri 标记，从而出现「应用到编辑器」按钮。这是纯文本层面的半自动
# 方案（HopGPT 上游不支持真正的 tool calling，无法全自动改文件）。
FORMAT_GUIDANCE = (
    "[System]\n"
    "You are helping a developer edit code in their IDE. "
    "When the request involves creating or modifying files, format your reply so the IDE can apply it directly:\n"
    "1. Group changes by file. Before each file's code, write a heading with the file's relative path, exactly like: ### src/example.ts\n"
    "2. Immediately after the heading, output ONE fenced code block (```language ... ```) containing the code.\n"
    "   - For a NEW file: include the complete file contents.\n"
    "   - For an EXISTING file: show only the changed region, and mark skipped lines with a short comment like // ... unchanged ...\n"
    "3. Use one code block per file. Use relative paths within the project.\n"
    "For questions that do NOT involve code changes, answer normally and ignore these formatting rules."
)


def resolve_endpoint(model_name: str) -> tuple[str, str]:
    """Map a client-facing model name to (endpoint, upstream model id)."""
    if model_name in MODELS:
        return MODELS[model_name]
    if model_name in MODEL_ALIASES:
        return MODELS[MODEL_ALIASES[model_name]]
    low = model_name.lower()
    if "claude" in low:
        return ("AnthropicClaude", "claude-sonnet-5-cached")
    if "gemini" in low:
        return ("google", model_name)
    if "llama" in low or "meta" in low:
        return ("MetaLlama", model_name)
    return MODELS[DEFAULT_MODEL]


def decode_jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data["exp"])
    except Exception:
        return None


def cred_status() -> dict:
    token, cookie = load_creds()
    exp = decode_jwt_exp(token) if token else None
    now = int(time.time())
    token_ok = bool(token) and exp is not None and exp > now + 60
    cookie_ok = "cf_clearance=" in cookie
    ready = token_ok  # token 有效即可尝试；cf_clearance 缺失会在实际请求时暴露
    hint = ""
    if not token:
        hint = "无 token：在浏览器打开 chat.ai.jh.edu，确保脚本/扩展在运行"
    elif not token_ok:
        hint = "token 已过期：刷新 chat.ai.jh.edu 页面，等脚本自动推送新凭证（约 10 秒）"
    elif not cookie_ok:
        hint = "token 就绪，但缺少 cf_clearance cookie（Cloudflare 可能拦截）。若请求被拦，请刷新 chat.ai.jh.edu 页面通过挑战。"
    return {
        "ready": ready,
        "token_len": len(token),
        "cookie_len": len(cookie),
        "token_expires_in": max(0, exp - now) if exp else 0,
        "cookie_ok": cookie_ok,
        "hint": hint,
    }


def load_creds() -> tuple[str, str]:
    token = ""
    cookie = ""
    if os.path.exists(TOKEN_FILE):
        token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    if os.path.exists(COOKIE_FILE):
        cookie = open(COOKIE_FILE, encoding="utf-8").read().strip()
    return token, cookie


def load_ua() -> str:
    if os.path.exists(UA_FILE):
        ua = open(UA_FILE, encoding="utf-8").read().strip()
        if ua:
            return ua
    return DEFAULT_UA


def parse_cookie(s: str) -> dict:
    out = {}
    for part in s.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def client(token: str, cookie: str):
    headers = {"User-Agent": load_ua()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return cffi_requests.Session(impersonate="chrome", headers=headers, cookies=parse_cookie(cookie))


def content_to_text(content) -> str:
    """Normalize OpenAI/Anthropic content (str or list of blocks) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and isinstance(p.get("text"), str):
                out.append(p["text"])
            elif p.get("type") in ("input_text", "output_text") and isinstance(p.get("text"), str):
                out.append(p["text"])
            # Anthropic image block: ignore binary but note its presence.
            elif p.get("type") == "image":
                out.append("[image]")
        return "".join(out)
    return str(content)


def normalize_messages(body: dict) -> list[dict]:
    """Accept Chat Completions (`messages`) or Cursor Agent / Responses API (`input`)."""
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return messages

    raw_input = body.get("input")
    if raw_input is None:
        return []

    if isinstance(raw_input, str):
        return [{"role": "user", "content": raw_input}]

    if isinstance(raw_input, list):
        out = []
        for item in raw_input:
            if isinstance(item, str):
                out.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role = item.get("role") or "user"
                if role == "developer":
                    role = "system"
                content = item.get("content", item.get("text", ""))
                out.append({"role": role, "content": content})
        return out

    return []


def messages_to_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = content_to_text(m.get("content", ""))
        tag = {"system": "[System]", "assistant": "[Assistant]"}.get(role, "[User]")
        parts.append(f"{tag}\n{content}")
    return "\n\n".join(parts).strip()


def parse_sse_body(body: str) -> tuple[str, str | None]:
    """Extract the assistant reply (and any upstream error) from a LibreChat SSE body."""
    deltas: list[str] = []
    final_text = ""

    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if raw in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if obj.get("event") == "on_message_delta":
            data = obj.get("data") or {}
            delta = data.get("delta") or {}
            for part in delta.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    deltas.append(part["text"])
            continue

        if not (obj.get("final") and isinstance(obj.get("responseMessage"), dict)):
            continue

        rm = obj["responseMessage"]
        content = rm.get("content") or []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "error":
                return "", c.get("error")
        text = rm.get("text") or ""
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                text += c["text"]
        final_text = text

    if final_text:
        return final_text, None
    if deltas:
        return "".join(deltas), None
    return "", None


app = FastAPI(title="hopgpt-openai-proxy")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def log_requests(request: Request, call_next):
    path = request.url.path
    if path not in ("/health", "/favicon.ico"):
        log.info("→ %s %s", request.method, path)
    response = await call_next(request)
    if path not in ("/health", "/favicon.ico"):
        log.info("← %s %s", response.status_code, path)
    return response


@app.get("/health")
def health():
    st = cred_status()
    return {"status": "ok" if st["ready"] else "degraded", **st}


# ---------------------------------------------------------------------------
# 浏览器执行任务队列：代理把上游请求交给浏览器扩展执行（浏览器是真实
# Chrome，天然通过 Cloudflare），扩展返回原始 SSE 文本，代理再翻译回
# OpenAI/Anthropic 格式。curl_cffi 路径作为扩展不在线时的兜底。
# ---------------------------------------------------------------------------
PENDING_JOBS: dict[str, dict] = {}
EXTENSION_PICKUP_TIMEOUT = 4.0   # 等待扩展领取任务的秒数
EXTENSION_EXEC_TIMEOUT = 240.0   # 等待扩展完成上游调用的秒数


@app.get("/jobs/poll")
def jobs_poll():
    """扩展轮询：返回最早的一个待执行任务（并标记为已领取）。"""
    for job_id, job in PENDING_JOBS.items():
        if not job.get("claimed"):
            job["claimed"] = True
            return {"job": {"job_id": job_id, **job["payload"]}}
    return {"job": None}


@app.post("/jobs/{job_id}/result")
async def jobs_result(job_id: str, request: Request):
    """扩展把上游原始响应回传到这里。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    job = PENDING_JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    job["result"] = body
    job["event"].set()
    return {"ok": True}


async def run_generation_via_extension(endpoint: str, model: str, text: str, token: str):
    """通过浏览器扩展执行上游调用。返回 (raw_sse_body, error)。"""
    job_id = uuid.uuid4().hex
    event = asyncio.Event()
    PENDING_JOBS[job_id] = {
        "payload": {
            "endpoint": endpoint,
            "model": model,
            "text": text,
            "endpointType": ENDPOINT_TYPES.get(endpoint),
            "token": token,
        },
        "claimed": False,
        "result": None,
        "event": event,
    }
    try:
        # 阶段 1：等扩展领取任务（短超时，判断扩展是否在线）
        for _ in range(int(EXTENSION_PICKUP_TIMEOUT * 10)):
            if PENDING_JOBS[job_id]["claimed"]:
                break
            await asyncio.sleep(0.1)
        if not PENDING_JOBS[job_id]["claimed"]:
            return None, "EXTENSION_ABSENT"

        # 阶段 2：等扩展完成上游调用并回传结果
        await asyncio.wait_for(event.wait(), timeout=EXTENSION_EXEC_TIMEOUT)
        result = PENDING_JOBS[job_id]["result"] or {}
        if not result.get("ok"):
            return None, result.get("error") or f"extension HTTP {result.get('status')}"
        return result.get("body", ""), None
    except asyncio.TimeoutError:
        return None, "EXTENSION_TIMEOUT"
    finally:
        PENDING_JOBS.pop(job_id, None)


def openai_error(message: str, status: int = 400, code: str = "api_error"):
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        status_code=status,
    )


@app.post("/__capture")
async def capture_creds(request: Request):
    """Accept fresh credentials POSTed from your own browser (extension)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    token = (body.get("token") or "").strip()
    cookie = (body.get("cookie") or "").strip()
    user_agent = (body.get("userAgent") or "").strip()
    token_ok = token.startswith("eyJ") and token.count(".") == 2 and len(token) > 100
    cookie_ok = "cf_clearance=" in cookie
    # 总是保存非空 cookie（即使没有 cf_clearance），带上所有会话 cookie 去请求，
    # 便于诊断，也让 __Secure-next-auth.session-token 等发挥作用。
    if cookie:
        open(COOKIE_FILE, "w", encoding="utf-8").write(cookie)
    if token and token_ok:
        open(TOKEN_FILE, "w", encoding="utf-8").write(token)
    if user_agent:
        open(UA_FILE, "w", encoding="utf-8").write(user_agent)
    # 返回 cookie 里有哪些键，方便排查
    cookie_names = [p.split("=", 1)[0] for p in cookie.split(";") if "=" in p]
    return {
        "ok": token_ok and cookie_ok,
        "token_len": len(token),
        "cookie_len": len(cookie),
        "token_valid": token_ok,
        "cookie_valid": cookie_ok,
        "cookie_names": cookie_names,
        "ua_saved": bool(user_agent),
    }


@app.get("/v1/models")
def list_models():
    data = [
        {"id": name, "object": "model", "created": int(time.time()), "owned_by": "hopgpt"}
        for name in MODELS
    ]
    return {"object": "list", "data": data}


def run_generation(endpoint: str, model: str, text: str, token: str, cookie: str):
    """Call LibreChat and return (content, error)."""
    s = client(token, cookie)
    payload = {
        "endpoint": endpoint,
        "model": model,
        "text": text,
        "conversationId": None,
        "isTemporary": False,
        "isContinued": False,
        "isRegenerate": False,
        "messageId": str(uuid.uuid4()),
        "parentMessageId": "00000000-0000-0000-0000-000000000000",
    }
    endpoint_type = ENDPOINT_TYPES.get(endpoint)
    if endpoint_type:
        payload["endpointType"] = endpoint_type

    try:
        r = s.post(f"{BASE_URL}/api/agents/chat/{endpoint}", json=payload, timeout=60)
    except Exception as e:
        return None, f"POST failed: {e}"

    if r.status_code >= 400:
        snippet = re.sub(r"<[^>]+>", " ", r.text[:500])
        snippet = re.sub(r"\s+", " ", snippet).strip()
        log.error("upstream %s → POST %s: %s (cookie_len=%d)",
                  endpoint, r.status_code, snippet[:200], len(cookie))
        if r.status_code == 401:
            return None, "CREDS_EXPIRED"
        if r.status_code == 403 or "Just a moment" in r.text:
            return None, "CLOUDFLARE_BLOCK"
        return None, f"POST {r.status_code}: {snippet[:300]}"

    try:
        j = r.json()
    except Exception:
        return None, f"POST bad json: {r.text[:300]}"

    stream_id = j.get("streamId")
    if not stream_id:
        return None, f"no streamId in response: {json.dumps(j)[:300]}"

    try:
        r2 = s.get(f"{BASE_URL}/api/agents/chat/stream/{stream_id}", timeout=180)
    except Exception as e:
        return None, f"stream GET failed: {e}"

    if r2.status_code >= 400:
        return None, f"stream {r2.status_code}"

    content, err = parse_sse_body(r2.text)
    if err:
        return None, f"upstream error: {err[:300]}"
    if not content:
        return None, f"empty response: {r2.text[:300]}"
    return content, None


# aider 等客户端自带严格的编辑协议（SEARCH/REPLACE、udiff 等），我们注入的
# FORMAT_GUIDANCE 会与之冲突，导致模型输出既不符合 aider 也不符合我们的格式。
# 检测到这类客户端时跳过引导，让它们的原始系统提示原封不动地传给上游。
_AIDER_MARKERS = (
    "SEARCH/REPLACE",
    "<<<<<<< SEARCH",
    ">>>>>>> REPLACE",
    "Act as an expert software engineer",
    "Act as an expert code analyst",
)


def _should_inject_format_guidance(text: str) -> bool:
    return not any(marker in text for marker in _AIDER_MARKERS)


async def resolve_generation(endpoint: str, upstream: str, text: str, token: str, cookie: str):
    """统一入口：优先走浏览器扩展（真实 Chrome，通过 Cloudflare），
    扩展不在线时兜底 curl_cffi。返回 (content, error_code_or_message)。"""
    # 加格式引导，让代码修改类回答能被 VS Code 识别为「可应用」的代码块。
    # 但对 aider 等自带编辑协议的客户端跳过，避免冲突。
    if _should_inject_format_guidance(text):
        text = FORMAT_GUIDANCE + "\n\n" + text
    raw_body, err = await run_generation_via_extension(endpoint, upstream, text, token)
    if err == "EXTENSION_ABSENT":
        log.info("浏览器扩展不在线，回退到 curl_cffi 直连")
        return run_generation(endpoint, upstream, text, token, cookie)
    if err:
        return None, err
    content, perr = parse_sse_body(raw_body)
    if perr:
        return None, f"upstream error: {perr[:300]}"
    if not content:
        return None, f"empty response: {raw_body[:300]}"
    return content, None


# ---------------------------------------------------------------------------
# OpenAI Chat Completions API
# ---------------------------------------------------------------------------

def openai_chunk(model: str, text: str | None = None, finish: str | None = None) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    return await _chat_completions(body)


@app.post("/v1/responses")
async def openai_responses(request: Request):
    """Cursor Agent sometimes targets /v1/responses; we reuse chat completions."""
    body = await request.json()
    return await _chat_completions(body)


async def _chat_completions(body: dict):
    st = cred_status()
    if not st["ready"]:
        return openai_error(
            st["hint"] or "HopGPT credentials not ready",
            status=401,
            code="credentials_expired",
        )

    model_name = body.get("model") or DEFAULT_MODEL
    endpoint, upstream = resolve_endpoint(model_name)
    messages = normalize_messages(body)
    want_stream = bool(body.get("stream", False))

    text = messages_to_text(messages)
    if not text:
        return openai_error("empty prompt")

    token, cookie = load_creds()
    content, err = await resolve_generation(endpoint, upstream, text, token, cookie)
    if err:
        if err == "CREDS_EXPIRED":
            return openai_error(
                "HopGPT 凭证已过期。请刷新浏览器里的 chat.ai.jh.edu 页面，等 10 秒后重试。",
                status=401,
                code="credentials_expired",
            )
        if err == "CLOUDFLARE_BLOCK":
            return openai_error(
                "被 Cloudflare 拦截（缺少 cf_clearance）。请刷新 chat.ai.jh.edu 页面通过挑战，等扩展重新推送 cookie 后重试。",
                status=403,
                code="cloudflare_block",
            )
        if err == "EXTENSION_TIMEOUT":
            return openai_error(
                "浏览器扩展执行超时。请确认 chat.ai.jh.edu 页面开着、扩展已启用，然后重试。",
                status=502,
                code="extension_timeout",
            )
        log.error("upstream: %s", err[:200])
        return openai_error(f"HopGPT error: {err[:300]}", status=502)

    if not want_stream:
        return JSONResponse(
            {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    def gen():
        step = 120
        first = True
        for i in range(0, len(content), step):
            chunk = content[i:i + step]
            delta = {"content": chunk}
            if first:
                delta["role"] = "assistant"
                first = False
            payload = {
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps(openai_chunk(model_name, finish='stop'), ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Anthropic Messages API
# ---------------------------------------------------------------------------

def _anthropic_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def anthropic_response(model: str, content: str) -> dict:
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [_anthropic_text_block(content)],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    body = await request.json()
    return await _anthropic_messages(body)


async def _anthropic_messages(body: dict):
    st = cred_status()
    if not st["ready"]:
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error", "message": st["hint"]}},
            status_code=401,
        )

    model_name = body.get("model") or "claude-sonnet"
    endpoint, upstream = resolve_endpoint(model_name)
    want_stream = bool(body.get("stream", False))

    # Fold system + messages into a single turn.
    parts = []
    system = body.get("system")
    if system:
        parts.append(f"[System]\n{content_to_text(system)}")
    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = content_to_text(m.get("content", ""))
        tag = "[Assistant]" if role == "assistant" else "[User]"
        parts.append(f"{tag}\n{content}")
    text = "\n\n".join(parts).strip()

    token, cookie = load_creds()
    content, err = await resolve_generation(endpoint, upstream, text, token, cookie)
    if err:
        if err == "CREDS_EXPIRED":
            msg = "HopGPT 凭证已过期，请刷新 chat.ai.jh.edu"
        elif err == "CLOUDFLARE_BLOCK":
            msg = "被 Cloudflare 拦截（缺少 cf_clearance），请刷新 chat.ai.jh.edu 通过挑战"
        elif err == "EXTENSION_TIMEOUT":
            msg = "浏览器扩展执行超时，请确认 chat.ai.jh.edu 页面开着、扩展已启用"
        else:
            msg = err
        code = 401 if err == "CREDS_EXPIRED" else (403 if err == "CLOUDFLARE_BLOCK" else 502)
        return JSONResponse(
            {"type": "error", "error": {"type": "api_error", "message": msg[:300]}},
            status_code=code,
        )

    if not want_stream:
        return JSONResponse(anthropic_response(model_name, content))

    # Stream in Anthropic SSE event format.
    msg_id = f"msg_{uuid.uuid4().hex}"

    def gen():
        # message_start
        yield "event: message_start\n"
        yield "data: " + json.dumps({
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model_name,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }, ensure_ascii=False) + "\n\n"

        # content_block_start
        yield "event: content_block_start\n"
        yield "data: " + json.dumps({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }, ensure_ascii=False) + "\n\n"

        # content_block_delta(s)
        step = 120
        for i in range(0, len(content), step):
            yield "event: content_block_delta\n"
            yield "data: " + json.dumps({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": content[i:i+step]},
            }, ensure_ascii=False) + "\n\n"

        # content_block_stop
        yield "event: content_block_stop\n"
        yield "data: " + json.dumps({"type": "content_block_stop", "index": 0}, ensure_ascii=False) + "\n\n"

        # message_delta
        yield "event: message_delta\n"
        yield "data: " + json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        }, ensure_ascii=False) + "\n\n"

        # message_stop
        yield "event: message_stop\n"
        yield "data: " + json.dumps({"type": "message_stop"}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787)
