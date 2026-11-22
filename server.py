# -*- coding: utf-8 -*-
"""
wake_UP 自建前端 —— 后端服务
=============================
在 24 小时常亮的 Windows 上常驻运行。

  1. 内嵌 wake_UP 引擎 (deamo_wake)：管理情境 / 闹钟 / 主动唤醒
  2. 提供网页聊天接口，浏览器打开就能聊
  3. 到点或事件命中时，调模型跑一轮，把主动消息推回网页
  4. 对话历史存本地 JSON，重启不丢

关键设计：HTTP 线程只负责收发，所有引擎与模型调用都通过
run_coroutine_threadsafe 回到同一个 asyncio 主事件循环执行，
避免 asyncio.Lock 跨循环报错。

启动：python server.py
浏览器：http://127.0.0.1:8000
诊断：浏览器打开 http://127.0.0.1:8000/api/diag
"""

import asyncio
import json
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "chat-history.json"


def _load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    env = os.environ.get
    return {
        "api_base": (env("WAKE_API_BASE") or cfg.get("api_base") or "").rstrip("/"),
        "api_key": env("WAKE_API_KEY") or cfg.get("api_key") or "",
        "main_model": env("WAKE_MAIN_MODEL") or cfg.get("main_model") or "",
        "slm_model": env("WAKE_SLM_MODEL") or cfg.get("slm_model") or cfg.get("main_model") or "",
        "host": env("WAKE_HOST") or "0.0.0.0",
        "port": int(env("WAKE_PORT") or cfg.get("port") or 8000),
    }


CFG = _load_config()
API_BASE = CFG["api_base"]
API_KEY = CFG["api_key"]
MAIN_MODEL = CFG["main_model"]
SLM_MODEL = CFG["slm_model"] or MAIN_MODEL
HOST = CFG["host"]
PORT = CFG["port"]

from deamo_wake.engine import WakeEngine
from deamo_wake.storage import SQLiteStore
from deamo_wake import companion

CONV_ID = "frontend-chat"

MAIN_LOOP = None
engine = None

outbox = asyncio.Queue()
history = []
history_lock = threading.Lock()

LAST_ERROR = {"where": "", "message": ""}


# ---------------- 报错处理：把上游真实原因带出来 ----------------

class ApiError(RuntimeError):
    def __init__(self, status, body, url=""):
        self.status = status
        self.body = body or ""
        self.url = url
        super().__init__("HTTP %s: %s" % (status, brief_error(self.body)))

    def brief(self):
        return "HTTP %s %s" % (self.status, brief_error(self.body))


def brief_error(body):
    """从各种上游错误格式里提取一句人话。"""
    text = str(body or "").strip()
    if not text:
        return "(空响应)"
    try:
        data = json.loads(text)
    except Exception:
        return text[:400]
    cands = []
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            cands.append(err.get("message"))
            for v in err.values():
                if isinstance(v, str):
                    cands.append(v)
        elif isinstance(err, list) and err:
            first = err[0]
            if isinstance(first, dict):
                cands.append(first.get("message"))
        elif isinstance(err, str):
            cands.append(err)
        cands.append(data.get("message"))
        cands.append(data.get("detail"))
        for det in (data.get("details") or []) if isinstance(data.get("details"), list) else []:
            if isinstance(det, dict):
                cands.append(det.get("message"))
    for c in cands:
        if isinstance(c, str) and c.strip():
            return c.strip()[:400]
    return text[:400]


def _request(path, payload=None, timeout=180, method=None):
    url = API_BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode("utf-8", "replace"), url)
    except urllib.error.URLError as exc:
        raise ApiError(0, "连不上 %s：%s" % (API_BASE, exc.reason), url)
    except Exception as exc:
        raise ApiError(0, str(exc), url)


def _post(path, payload, timeout=180):
    return _request(path, payload, timeout)


def _get(path, timeout=60):
    return _request(path, None, timeout)


# ---------------- 模型调用（中转站，OpenAI 兼容） ----------------

async def chat_api(model, messages, tools=None, temperature=0.5, max_tokens=1500):
    if not model:
        raise ApiError(0, "没有配置模型 id，请编辑 config.json")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    st, data = await asyncio.to_thread(_post, "/chat/completions", payload)
    try:
        return data["choices"][0]["message"]
    except Exception:
        raise ApiError(st, json.dumps(data, ensure_ascii=False), API_BASE + "/chat/completions")


def extract_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"```\s*$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except Exception:
            pass
    return {}


def _slm_fallback(system):
    s = (system or "").lower()
    if "delta" in s or "scene" in s:
        return {"deltas": [], "reason": "slm_failed"}
    if "wake_master" in s or "patrol" in s:
        return {"wake_master": False, "reason": "slm_failed", "urgency": "low"}
    return {"ok": False, "reason": "slm_failed"}


async def host_slm(*, system, user_content, max_tokens, temperature) -> dict:
    """SLM：情境识别 / wake_when 翻译 / 巡检判断。必须返回 dict。"""
    try:
        msg = await chat_api(
            SLM_MODEL,
            [{"role": "system", "content": system},
             {"role": "user", "content": user_content}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        data = extract_json(msg.get("content") or "")
        return data if isinstance(data, dict) and data else _slm_fallback(system)
    except Exception as exc:
        LAST_ERROR["where"] = "slm"
        LAST_ERROR["message"] = str(exc)
        print("[slm] failed:", exc)
        return _slm_fallback(system)


# ---------------- 工具调用循环 ----------------

def openai_tools(eng):
    out = []
    for schema in (eng.tool_schemas if eng else []):
        fn = schema.get("function") if "function" in schema else schema
        out.append({
            "type": "function",
            "function": {
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": fn.get("inputSchema") or {"type": "object", "properties": {}},
            },
        })
    return out


async def run_model_turn(messages, tool_schemas, dispatch, max_rounds=6):
    """跑一轮模型对话，支持工具调用循环。若中转站不支持 tools 则退回纯文本。"""
    use_tools = bool(tool_schemas)
    for _ in range(max_rounds):
        try:
            msg = await chat_api(MAIN_MODEL, messages,
                                 tools=tool_schemas if use_tools else None)
        except ApiError as exc:
            if use_tools and exc.status in (400, 404, 422):
                # 有些中转站不支持 tools 参数，去掉再试一次。
                use_tools = False
                print("[tools] 不被支持，退回纯文本:", exc)
                messages.append({"role": "system",
                                 "content": "（本轮没有工具可用，请直接用文字说明你建议的安排。）"})
                continue
            raise
        content = msg.get("content") or ""
        calls = msg.get("tool_calls") or []
        assistant = {"role": "assistant", "content": content or None}
        if calls:
            assistant["tool_calls"] = calls
        messages.append(assistant)
        if not calls:
            return content
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            try:
                result = await dispatch(name, args)
            except Exception as exc:
                result = {"error": str(exc)}
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result,
            })
    return ""


# ---------------- 主动唤醒的 runner ----------------

WAKE_SYSTEM = (
    "你是这个聊天应用里的助手，现在被主动唤醒机制叫醒了。"
    "根据唤醒报告判断：要不要主动联系用户，或调整下一次唤醒安排。"
    "要联系用户就用 notify_user 工具发出内容；"
    "要调整安排就用 collar_companion 工具。"
    "如果你判断现在没必要打扰，就只更新安排，不要发消息。"
)


async def host_runner(request, on_accepted, tools, interrupted):
    try:
        await on_accepted(request.get("native_thread_id") or "wake-frontend-thread")
    except Exception:
        pass
    if interrupted.is_set():
        return {"accepted": True, "completed": False, "error": "interrupted"}

    report = request.get("report") or ""
    trigger = request.get("trigger") or "wake"

    msgs = [{"role": "system", "content": WAKE_SYSTEM}]
    with history_lock:
        msgs.extend(history[-20:])
    msgs.append({"role": "user",
                 "content": "【唤醒报告｜触发：" + str(trigger) + "】\n" + report})

    async def dispatch(name, args):
        return await tools(name, args)

    try:
        text = await run_model_turn(msgs, openai_tools(engine), dispatch)
    except Exception as exc:
        LAST_ERROR["where"] = "wake"
        LAST_ERROR["message"] = str(exc)
        return {"accepted": True, "completed": False, "error": str(exc)}

    if text:
        with history_lock:
            history.append({"role": "assistant", "content": text})
            save_history()
    return {"accepted": True, "completed": True, "substantive": bool(text), "text": text}


async def host_notifier(conversation_id, message):
    """主动消息出口：推给网页。"""
    await outbox.put({"conversation_id": conversation_id, "message": message})
    return {"delivered": True}


# ---------------- 普通聊天 ----------------

CHAT_SYSTEM = (
    "你是这个聊天应用里的助手。你可以调用工具安排『主动唤醒』：\n"
    "- collar_companion：管理情境、闹钟、重复节奏、短期字条。\n"
    "  『每隔十分钟来找我一次』→ set_repeat（cadence_min=10）；"
    "『九点叫我』→ set_alarm（at=ISO 时间）；"
    "『我出门了』→ start_scene 且 scene_type=outing；"
    "『回来了』→ end_scene。\n"
    "- notify_user：主动给用户发消息。\n"
    "识别到这类意图时务必调用工具登记，不要只是口头答应。"
    "其余问题正常回答。"
)


async def dispatch_normal(name, args):
    if name == "notify_user":
        msg = str((args or {}).get("message") or "").strip()
        if msg:
            await outbox.put({"conversation_id": CONV_ID, "message": msg})
        return {"sent": bool(msg)}
    return await engine.tool(name, args, CONV_ID)


async def handle_user_message(text):
    with history_lock:
        history.append({"role": "user", "content": text})
        save_history()

    try:
        stamp = datetime.now(timezone.utc)
        msg_id = "web-" + str(len(history)) + "-" + str(int(stamp.timestamp() * 1000))
        await engine.user_message(CONV_ID, msg_id, text, at=stamp)
    except Exception as exc:
        print("[user_message] failed:", exc)

    msgs = [{"role": "system", "content": CHAT_SYSTEM}]
    with history_lock:
        msgs.extend(list(history))

    async def dispatch(name, args):
        return await dispatch_normal(name, args)

    try:
        reply = await run_model_turn(msgs, openai_tools(engine), dispatch)
        LAST_ERROR["where"] = ""
        LAST_ERROR["message"] = ""
    except Exception as exc:
        LAST_ERROR["where"] = "chat"
        LAST_ERROR["message"] = str(exc)
        reply = ("（模型调用失败）\n" + str(exc)
                 + "\n\n提示：打开 http://127.0.0.1:8000/api/diag 可以看到中转站返回的真实原因，"
                   "以及它支持哪些模型名。")

    with history_lock:
        history.append({"role": "assistant", "content": reply})
        save_history()
    try:
        await engine.assistant_activity(at=datetime.now(timezone.utc))
    except Exception:
        pass
    return reply


# ---------------- 历史持久化 ----------------

def load_history():
    global history
    if STATE_FILE.exists():
        try:
            history = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            history = []


def save_history():
    try:
        STATE_FILE.write_text(
            json.dumps(history[-200:], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print("[history] save failed:", exc)


# ---------------- 诊断：一眼看出 key / 地址 / 模型哪个不对 ----------------

def diagnose():
    """检查配置、列出中转站支持的模型、并实测当前模型能否调用。"""
    out = {
        "api_base": API_BASE,
        "main_model": MAIN_MODEL,
        "slm_model": SLM_MODEL,
        "key_present": bool(API_KEY),
        "key_preview": (API_KEY[:6] + "..." + API_KEY[-4:]) if API_KEY else "",
        "models": [],
        "models_error": "",
        "chat_ok": None,
        "chat_error": "",
        "suggestion": "",
    }

    if not API_BASE:
        out["suggestion"] = "config.json 里没有 api_base。"
        return out
    if not API_KEY:
        out["suggestion"] = "config.json 里没有 api_key。"
        return out

    # 1) 中转站支持哪些模型
    try:
        st, data = await0(_get, "/models")
        ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
        out["models"] = ids[:300]
        out["models_count"] = len(ids)
    except Exception as exc:
        out["models_error"] = str(exc)

    # 2) 当前模型能不能真的调通
    if MAIN_MODEL:
        try:
            await0(_post, "/chat/completions", {
                "model": MAIN_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 16,
            })
            out["chat_ok"] = True
        except Exception as exc:
            out["chat_ok"] = False
            out["chat_error"] = str(exc)

    # 3) 给结论
    if out["chat_ok"] is True:
        out["suggestion"] = "一切正常，可以直接聊天。"
    elif out["models"]:
        hit = [m for m in out["models"] if MAIN_MODEL and MAIN_MODEL.lower() in str(m).lower()]
        if hit:
            out["suggestion"] = "模型名接近可用值，试试：" + ", ".join(hit[:5])
        else:
            out["suggestion"] = ("当前模型 %s 不在中转站清单里。"
                                 "请把 main_model 改成下面清单里的一个名字。" % MAIN_MODEL)
    elif out["chat_error"]:
        out["suggestion"] = "看 chat_error 里的真实原因。"
    return out


def await0(fn, *args):
    """同步小助手：把 (fn, *args) 直接执行。"""
    return fn(*args)


# ---------------- HTTP 层 ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                html = (BASE_DIR / "index.html").read_bytes()
            except Exception:
                html = b"<h1>index.html not found</h1>"
            self._send(200, html, "text/html")
        elif path == "/poll":
            async def wait_one():
                try:
                    return await asyncio.wait_for(outbox.get(), timeout=25)
                except asyncio.TimeoutError:
                    return None
            item = asyncio.run_coroutine_threadsafe(wait_one(), MAIN_LOOP).result(timeout=35)
            self._send(200, item or {"none": True})
        elif path in ("/api/status", "/api/health"):
            async def status():
                try:
                    st = await companion.get_companion_status(engine.store)
                    settings = await engine.store.get_setting("collar_wake")
                    return {
                        "ok": True,
                        "configured": bool(API_BASE and API_KEY and MAIN_MODEL),
                        "model": MAIN_MODEL,
                        "api_base": API_BASE,
                        "active_scenes": st.get("active_scenes", []),
                        "recent_scenes": st.get("recent_scenes", [])[-8:],
                        "settings": settings,
                        "last_error": LAST_ERROR,
                    }
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}
            self._send(200, asyncio.run_coroutine_threadsafe(status(), MAIN_LOOP).result(timeout=20))
        elif path == "/api/diag":
            def run_diag():
                return diagnose()
            self._send(200, asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(run_diag), MAIN_LOOP).result(timeout=90))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, {"error": "bad request"})
            return

        if path == "/message":
            text = str(data.get("text") or "").strip()
            if not text:
                self._send(400, {"error": "empty text"})
                return
            reply = asyncio.run_coroutine_threadsafe(
                handle_user_message(text), MAIN_LOOP).result(timeout=240)
            self._send(200, {"reply": reply})
        elif path == "/api/tool":
            action = str(data.get("action") or "")
            if not action:
                self._send(400, {"error": "action required"})
                return
            args = dict(data.get("args") or {})
            args["action"] = action
            try:
                result = asyncio.run_coroutine_threadsafe(
                    engine.tool("collar_companion", args, CONV_ID), MAIN_LOOP).result(timeout=30)
            except Exception as exc:
                result = {"error": str(exc)}
            self._send(200, result)
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


# ---------------- 启动 ----------------

async def main():
    global MAIN_LOOP, engine
    MAIN_LOOP = asyncio.get_running_loop()
    load_history()

    store = SQLiteStore(BASE_DIR / "wake-state.sqlite3")
    engine = WakeEngine(
        store,
        slm=host_slm,
        runner=host_runner,
        notifier=host_notifier,
        known_places={"home": {"label": "家", "role": "home"},
                      "work": {"label": "公司", "role": "work"}},
        known_apps=[],
    )
    await engine.initialize()
    await engine.configure(enabled=True, wake_live=True)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    print("=" * 60)
    print(" wake_UP 自建前端 已启动")
    print("   本机     : http://127.0.0.1:%d" % PORT)
    print("   局域网   : http://192.168.31.127:%d" % PORT)
    print("   模型     : %s  @  %s" % (MAIN_MODEL, API_BASE))
    print("   诊断     : http://127.0.0.1:%d/api/diag" % PORT)
    if not (API_BASE and API_KEY and MAIN_MODEL):
        print("   [警告] 配置不完整，请编辑 config.json 后重启")
    print("=" * 60)

    async def patrol():
        """后台巡检：每 60 秒检查一次到期闹钟、情境与事件。"""
        while True:
            try:
                result = await engine.tick()
                if result and not result.get("skipped"):
                    print("[tick]", result)
            except Exception as exc:
                print("[tick] failed:", exc)
            await asyncio.sleep(60)

    await asyncio.gather(
        asyncio.to_thread(server.serve_forever),
        patrol(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止")
