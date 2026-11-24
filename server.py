#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STALKER 翻译 MCP 服务（Hermes Agent 客户端）。

只提供「翻译大脑」：术语库 + 提示词 + 翻译管线 + X-Ray 文件处理。
不启动、不下载、不管理任何模型——推理请求转发给用户自己的 OpenAI 兼容端点
（Hermes 部署的本地模型，或 llama.cpp / LM Studio / Ollama 等）。

协议：MCP stdio（换行分隔 JSON-RPC），零第三方依赖。
工具：check_env / translate_mod / translate_text
"""

import contextlib
import importlib.util
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KERNEL_DIR = os.path.join(HERE, "kernel")
KERNEL_PY = os.path.join(KERNEL_DIR, "plugin.py")
LOG_DIR = os.path.join(HERE, "logs")
CONFIG_PATH = (os.environ.get("STALKER_MCP_CONFIG")
               or os.path.join(HERE, "config.json"))
SERVER_NAME = "stalker-translate"
VERSION = "1.0.0"

DEFAULT_CONFIG = {
    # OpenAI 兼容端点。Hermes 的默认端点是它自己拉起的 llama-server，
    # 安装脚本会自动从 Hermes 的 config.yaml 读出来写入 config.json。
    "base_url": "http://127.0.0.1:18434/v1",
    "api_key": "",
    # 端点上的模型名；llama.cpp 会忽略该字段，vLLM/Ollama 等需要填对。
    "model": "local-model",
    # 每条文本可用的上下文预算；超出会自动切句。
    "ctx_size": 8192,
    # 并发请求数（llama-server 的 --parallel 槽位数）。
    "parallel": 1,
    # 单次请求超时（秒）。
    "timeout_sec": 300,
    # 追加到系统提示词的要求，例如 “专有名词保留英文”。
    "prompt_extra": "",
    "official_prompts": False,
    "source_lang": "auto",
    "target_lang": "chs",
}

# ── 协议通道 ────────────────────────────────────────────────────────
# 先把真正的 stdout 复制出来专供 JSON-RPC，再把 fd 1 指向 stderr。
# 之后内核/解析器里的任何 print 都只会进日志，不会污染协议流。
_PROTO_OUT = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n",
                       buffering=1)
_PROTO_IN = sys.stdin.buffer
os.dup2(2, 1)
sys.stdout = sys.stderr

sys.dont_write_bytecode = True

_SEND_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()
_KERNEL = {"mod": None}
_ENGINE = {"obj": None}
_TEXT_GLOSS = {"index": None, "pending": False}


# ── 基础工具 ────────────────────────────────────────────────────────

def _log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "mcp.log")
        with _LOG_LOCK:
            if os.path.isfile(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                try:
                    os.replace(path, path + ".1")
                except OSError:
                    pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _send(obj):
    data = json.dumps(obj, ensure_ascii=False)
    with _SEND_LOCK:
        _PROTO_OUT.write(data + "\n")
        _PROTO_OUT.flush()


def _ok(msg_id, result):
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _err(msg_id, code, message):
    _send({"jsonrpc": "2.0", "id": msg_id,
           "error": {"code": code, "message": message}})


def _notify(method, params):
    _send({"jsonrpc": "2.0", "method": method, "params": params})


class ToolError(Exception):
    """工具可预期的失败（参数/环境问题），直接回给客户端。"""


# ── 配置与内核 ──────────────────────────────────────────────────────

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = CONFIG_PATH
    if not os.path.isfile(path):
        example = os.path.join(HERE, "config.example.json")
        if os.path.isfile(example):
            path = example
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except FileNotFoundError:
        pass
    except Exception as e:
        _log("读取配置失败（用默认值）：%s: %s" % (type(e).__name__, e))
    cfg["base_url"] = str(cfg.get("base_url") or "").strip()
    cfg["model"] = str(cfg.get("model") or "").strip()
    cfg["api_key"] = str(cfg.get("api_key") or "").strip()
    return cfg


def _load_kernel():
    if _KERNEL["mod"] is not None:
        return _KERNEL["mod"]
    if not os.path.isfile(KERNEL_PY):
        raise ToolError("内核缺失：%s（请重新运行 install.ps1）" % KERNEL_PY)
    spec = importlib.util.spec_from_file_location(
        "stalker_translate_kernel", KERNEL_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(mod)
    _KERNEL["mod"] = mod
    return mod


def _strip_v1(base):
    base = str(base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


class RemoteEngine(object):
    """把内核的翻译管线接到 OpenAI 兼容端点。

    只实现 TranslationJob 用到的那部分接口（translate / translate_batch /
    is_running / ctx_size / parallel / prompt_extra），不启动任何进程。
    """

    def __init__(self, plugin, cfg):
        self.plugin = plugin
        self.base = _strip_v1(cfg.get("base_url"))
        self.model = cfg.get("model") or "local-model"
        self.model_name = self.model
        self.ctx_size = max(512, int(cfg.get("ctx_size") or 8192))
        self.parallel = max(1, int(cfg.get("parallel") or 1))
        self.timeout_sec = max(10, int(cfg.get("timeout_sec") or 300))
        self.prompt_extra = str(cfg.get("prompt_extra") or "")
        self.official_prompts = bool(cfg.get("official_prompts", False))
        self.tier = "remote"
        self.variant = "remote"
        self.health_warn = ""
        self.gpu_verified = True
        self._auth = str(cfg.get("api_key") or "").strip()

    # 内核只读这些属性；所有请求都真发到远端，所以恒为“运行中”。
    def is_running(self):
        return True

    def stop(self):
        pass

    def translate(self, text, src_lang, tgt_lang, **kw):
        kw.setdefault("timeout_sec", self.timeout_sec)
        return self.plugin.LlamaServer.translate(
            self, text, src_lang, tgt_lang, **kw)

    def translate_batch(self, lines, src_lang, tgt_lang, **kw):
        kw.setdefault("timeout_sec", self.timeout_sec)
        return self.plugin.LlamaServer.translate_batch(
            self, lines, src_lang, tgt_lang, **kw)


def _get_engine():
    if _ENGINE["obj"] is not None:
        return _ENGINE["obj"]
    plugin = _load_kernel()
    eng = RemoteEngine(plugin, load_config())
    # 端点若要 API Key：内核的推理请求统一走模块级 _http_post，
    # 在这里包一层补 Authorization，避免再改内核。
    if eng._auth:
        original = plugin._http_post

        def post(url, data, headers=None, timeout=30.0):
            h = dict(headers or {})
            if "/v1/" in str(url):
                h.setdefault("Authorization", "Bearer " + eng._auth)
            return original(url, data, headers=h, timeout=timeout)

        plugin._http_post = post
    _ENGINE["obj"] = eng
    return eng


def _get_text_gloss():
    """整句翻译用的术语索引（与插件 UI 同一份合并词库）。"""
    if _TEXT_GLOSS["index"] is not None:
        return _TEXT_GLOSS["index"]
    plugin = _load_kernel()
    with contextlib.redirect_stdout(sys.stderr):
        layers = plugin.load_term_layers()
        merged, _user_set, _deleted, _clear = plugin.load_glossary_layers()
    _TEXT_GLOSS["index"] = plugin.GlossaryIndex(
        merged, case_sensitive=layers.get("case_sensitive"),
        variants=layers.get("variants"))
    return _TEXT_GLOSS["index"]


# ── 端点探测 ────────────────────────────────────────────────────────

def _http_json(url, api_key="", timeout=8):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "ignore")
        try:
            return r.status, json.loads(body)
        except Exception:
            return r.status, body


def _probe_endpoint(cfg):
    base = _strip_v1(cfg.get("base_url"))
    if not base:
        return {"ok": False, "error": "config.json 未填写 base_url"}
    key = cfg.get("api_key") or ""
    info = {"base_url": base + "/v1", "models": [], "ok": False}
    try:
        status, data = _http_json(base + "/v1/models", key)
        if status == 200 and isinstance(data, dict):
            info["ok"] = True
            info["models"] = [str(m.get("id")) for m in (data.get("data") or [])
                              if isinstance(m, dict)]
    except urllib.error.HTTPError as e:
        info["error"] = "HTTP %d：%s" % (e.code, base + "/v1/models")
    except Exception as e:
        info["error"] = "%s: %s" % (type(e).__name__, e)
    if not info["ok"]:
        # llama.cpp 的 /health 也能判断端点是否活着（老版本没有 /v1/models）。
        try:
            status, _data = _http_json(base + "/health", key, timeout=5)
            info["ok"] = status == 200
            info["health"] = status
            info.pop("error", None)
        except Exception:
            pass
    want = cfg.get("model") or ""
    if info["ok"] and want and info["models"] and want not in info["models"]:
        info["model_mismatch"] = want
    return info


# ── 工具实现 ────────────────────────────────────────────────────────

def _json_text(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


def tool_check_env(args):
    cfg = load_config()
    plugin = _load_kernel()
    layers = plugin.load_term_layers()
    out = {
        "ready": False,
        "server": {"name": SERVER_NAME, "version": VERSION},
        "kernel": plugin.PLUGIN_INFO.get("version", "?"),
        "python": sys.version.split()[0],
        "config_file": CONFIG_PATH,
        "endpoint": _probe_endpoint(cfg),
        "model": cfg.get("model") or "",
        "termbase": {
            "canon": len(layers.get("canon") or ()),
            "names": len(layers.get("names") or ()),
            "technical": len(layers.get("technical") or {}),
            "reference": len(layers.get("reference") or {}),
        },
    }
    missing = []
    if not out["endpoint"].get("ok"):
        missing.append("本地模型端点不可达（%s）。请先在 Hermes 中启动本地模型"
                       "（参考安装教程），或修改 config.json 的 base_url。"
                       % (out["endpoint"].get("error") or "连接失败"))
    try:
        plugin._load_vendor()
    except Exception as e:
        missing.append("X-Ray 解析器不可用：%s" % e)
    if out["termbase"]["canon"] < 100:
        missing.append("术语库为空或损坏，请重新运行 install.ps1")
    if args.get("deep") and out["endpoint"].get("ok"):
        try:
            eng = _get_engine()
            out["probe_translation"] = eng.translate(
                "Hello, stalker.", "eng", "chs", timeout_sec=60)
        except Exception as e:
            missing.append("模型可用但翻译探测失败：%s: %s"
                           % (type(e).__name__, e))
    out["ready"] = not missing
    if missing:
        out["problems"] = missing
    return out


def tool_translate_text(args):
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("text 不能为空")
    if len(text) > 20000:
        raise ToolError("单次文本过长（%d 字符）。MOD 请用 translate_mod"
                        % len(text))
    cfg = load_config()
    plugin = _load_kernel()
    tgt = str(args.get("target_lang") or cfg.get("target_lang") or "chs")
    src = str(args.get("source_lang") or cfg.get("source_lang") or "auto")
    exact = plugin.termbase_exact_translation(text)
    if exact is not None:
        return {"translation": exact, "source": text, "term_exact": True}
    eng = _get_engine()
    hints = _get_text_gloss().hits(text)
    try:
        out = eng.translate(text, src, tgt, glossary_hits=hints or None,
                            context=str(args.get("context") or "") or None)
    except Exception as e:
        raise ToolError("翻译请求失败：%s: %s。%s"
                        % (type(e).__name__, e, _endpoint_hint(cfg)))
    return {"translation": out, "source": text, "model": eng.model,
            "term_hits": [[k, v] for k, v in (hints or [])]}


def _endpoint_hint(cfg):
    return ("请确认本地模型已在 Hermes 中启动且 %s 可访问"
            % (cfg.get("base_url") or "端点"))


def tool_translate_mod(args):
    plugin = _load_kernel()
    cfg = load_config()
    in_dir = str(args.get("input_dir") or "").strip().strip('"')
    if not in_dir or not os.path.isdir(in_dir):
        raise ToolError("input_dir 不是有效目录：%s" % (in_dir or "(空)"))
    out_dir = str(args.get("output_dir") or "").strip()
    if not out_dir:
        out_dir = os.path.join(in_dir, "translated_hy_mt2")
    mode = str(args.get("mode") or "auto")
    if mode not in ("auto", "text", "cfgxml", "ltx", "scriptL", "scriptE"):
        raise ToolError("不支持的 mode：%s" % mode)
    src = str(args.get("source_lang") or cfg.get("source_lang") or "auto")
    tgt = str(cfg.get("target_lang") or "chs")

    eng = _get_engine()
    parser = plugin._load_vendor()
    logs = []

    def log(msg, tag="info"):
        line = str(msg)
        logs.append(line)
        if len(logs) > 400:
            del logs[:200]
        _log("[job] " + line)

    token = args.get("_progress_token")
    last_emit = {"t": 0.0}

    def progress(d):
        if not token:
            return
        now = time.time()
        if now - last_emit["t"] < 1.5:
            return
        last_emit["t"] = now
        total = int(d.get("files") or 0)
        idx = int(d.get("file_idx") or 0)
        _notify("notifications/progress", {
            "progressToken": token,
            "progress": idx, "total": total,
            "message": "%d/%d %s" % (idx, total, d.get("file") or ""),
        })

    merged, user_set, _deleted, _clear = plugin.load_glossary_layers()
    hard = set(plugin.load_hard_glossary_keys()) | set(user_set)

    job = plugin.TranslationJob(
        parser, eng, in_dir, mode, src, tgt,
        glossary=dict(merged),
        force_glossary=True,
        out_dir=out_dir,
        log=log, detail=log, progress=progress,
        reference_mode=True, copy_reference=True,
        only_referenced=False, protect_proper=True,
        preview_reference=False,
        hard_glossary_keys=hard or None,
        translate_literals=True, auto_session_terms=True,
        tm_lite=True, freq_names=True,
        parallel=eng.parallel)
    t0 = time.time()
    error = ""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            job.run()
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, e)
        _log("翻译任务异常：\n" + traceback.format_exc())
    elapsed = time.time() - t0
    warns = [x for x in logs if "错误" in x or "失败" in x or "警告" in x
             or "⚠" in x]
    result = {
        "status": "error" if error else "done",
        "input_dir": in_dir,
        "output_dir": job.out_dir,
        "elapsed_sec": round(elapsed, 1),
        "requests": int(getattr(job, "req_count", 0) or 0),
        "translated_chars": int(getattr(job, "trans_chars", 0) or 0),
        "failed_strings": int(getattr(job, "failed_strings", 0) or 0),
        "files": (job.batch_stats or {}).get("batches"),
        "model": eng.model,
        "log_tail": logs[-30:],
    }
    if error:
        result["error"] = error
        result["hint"] = _endpoint_hint(cfg)
    if warns:
        result["warnings"] = warns[-10:]
    return result


# ── MCP 协议 ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "check_env",
        "description": ("检查 STALKER 汉化环境：本地模型端点是否可达、模型名、"
                        "术语库与 X-Ray 解析器是否就绪。首次使用或翻译报错时"
                        "先调用它。deep=true 会真发一句测试翻译（较慢）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "deep": {"type": "boolean", "default": False,
                         "description": "是否真发一句测试翻译"},
            },
        },
    },
    {
        "name": "translate_mod",
        "description": ("汉化一个完整的 X-Ray / STALKER MOD 目录：自动识别文本 "
                        "XML、cfgxml、ltx、script，套用内置术语库和强制译名，"
                        "写出可用的汉化补丁。用户说“翻译/汉化这个 MOD”时调用。"
                        "翻译结果写入 output_dir（默认是输入目录下的 "
                        "translated_hy_mt2），可重复运行续译。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input_dir": {"type": "string",
                              "description": "MOD 根目录（含 configs/text 等）"},
                "output_dir": {"type": "string",
                               "description": "输出目录；省略则用 "
                                              "input_dir/translated_hy_mt2"},
                "mode": {"type": "string", "enum": ["auto", "text"],
                         "default": "auto",
                         "description": "auto=全部文本类型（推荐），text=只译文本 XML"},
                "source_lang": {
                    "type": "string", "enum": ["auto", "rus", "eng", "ukr"],
                    "default": "auto", "description": "源语言；一般 auto"},
            },
            "required": ["input_dir"],
        },
    },
    {
        "name": "translate_text",
        "description": ("翻译一小段 STALKER 文本（对话、任务描述、物品说明），"
                        "自动套用术语库。整段长文本请用 translate_mod。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待翻译文本"},
                "context": {"type": "string",
                            "description": "可选上下文（人物/场景），只帮助理解"},
                "source_lang": {"type": "string",
                                "enum": ["auto", "rus", "eng", "ukr"],
                                "default": "auto"},
                "target_lang": {"type": "string",
                                "enum": ["chs", "eng", "rus", "ukr"],
                                "default": "chs"},
            },
            "required": ["text"],
        },
    },
]


def _tool_result(obj, is_error=False):
    return {"content": [{"type": "text", "text": _json_text(obj)}],
            "isError": bool(is_error)}


def _handle_tool_call(params):
    name = params.get("name")
    args = dict(params.get("arguments") or {})
    token = (params.get("_meta") or {}).get("progressToken")
    if token:
        args["_progress_token"] = token
    handlers = {
        "check_env": tool_check_env,
        "translate_mod": tool_translate_mod,
        "translate_text": tool_translate_text,
    }
    fn = handlers.get(name)
    if fn is None:
        return _tool_result({"error": "未知工具：%s" % name}, True)
    try:
        return _tool_result(fn(args))
    except ToolError as e:
        return _tool_result({"error": str(e)}, True)
    except Exception as e:
        _log("工具 %s 异常：\n%s" % (name, traceback.format_exc()))
        return _tool_result({"error": "%s: %s" % (type(e).__name__, e)}, True)


def _handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id", None)
    params = msg.get("params") or {}
    is_notification = "id" not in msg
    _log("<- %s" % (method or "?"))
    if method == "initialize":
        _ok(msg_id, {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": ("STALKER 汉化翻译。用户要汉化 MOD 时先调用 "
                             "check_env，再按用户给的目录调用 translate_mod；"
                             "只翻译一段文本用 translate_text。"),
        })
    elif method in ("notifications/initialized", "initialized"):
        return
    elif method == "ping":
        _ok(msg_id, {})
    elif method == "tools/list":
        _ok(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        _ok(msg_id, _handle_tool_call(params))
    elif is_notification:
        return
    else:
        _err(msg_id, -32601, "未实现的方法：%s" % method)


def main():
    _log("MCP 服务启动 pid=%d python=%s" % (os.getpid(),
                                            sys.version.split()[0]))
    try:
        _load_kernel()
        _log("内核就绪：%s" % os.path.join(KERNEL_DIR, "plugin.py"))
    except Exception as e:
        _log("内核加载失败（工具调用时会再试）：%s: %s" % (type(e).__name__, e))
    while True:
        try:
            raw = _PROTO_IN.readline()
        except KeyboardInterrupt:
            break
        if not raw:
            break
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception as e:
            _log("无法解析的请求：%s" % e)
            continue
        if not isinstance(msg, dict):
            continue
        try:
            _handle(msg)
        except Exception:
            _log("处理请求异常：\n" + traceback.format_exc())
            msg_id = msg.get("id")
            if msg_id is not None:
                _err(msg_id, -32603, "内部错误，详见 logs/mcp.log")
    _log("MCP 服务退出")


if __name__ == "__main__":
    main()
