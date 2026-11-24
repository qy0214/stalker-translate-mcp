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
import html
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
KERNEL_DIR = os.path.join(HERE, "kernel")
KERNEL_PY = os.path.join(KERNEL_DIR, "plugin.py")
LOG_DIR = os.path.join(HERE, "logs")
CONFIG_PATH = (os.environ.get("STALKER_MCP_CONFIG")
               or os.path.join(HERE, "config.json"))
SERVER_NAME = "stalker-translate"
VERSION = "1.1.10"

DEFAULT_CONFIG = {
    # OpenAI 兼容端点。默认按 llama.cpp 的 8080；Hermes 安装脚本会写入它的
    # 18434 路由地址。
    "base_url": "http://127.0.0.1:8080/v1",
    "api_key": "",
    # 端点上的模型名；llama.cpp 会忽略该字段，vLLM/Ollama 等需要填对。
    "model": "local-model",
    # 每条文本可用的上下文预算；超出会自动切句。
    "ctx_size": 8192,
    # 并发请求数（llama-server 的 --parallel 槽位数）。
    "parallel": 1,
    # 翻译开始时自动打开实时进度页（显示当前文件、原文→译文）。
    "open_progress_page": True,
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
_PROGRESS_LOCK = threading.Lock()
_KERNEL = {"mod": None}
_ENGINE = {"obj": None}
_TEXT_GLOSS = {"index": None, "pending": False}
_IN_Q = queue.Queue()
_CLIENT = {"caps": {}, "name": ""}
_REQ_SEQ = [0]
_PLAN_CACHE = {"key": None, "plan": None}
_GPU_CACHE = {"done": False, "name": "", "vram_mb": 0}


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


_NOTIFY_LOG = {"ts": 0.0}


def _mcp_log(level, text):
    """把关键日志作为 MCP logging 通知发给客户端。

    Hermes 会把 notifications/message 写进 agent.log（MCP server log [...]），
    比只写本地 mcp.log 更容易被用户看到；info 限频 1 秒，避免刷屏。
    """
    text = str(text or "").strip()
    if not text:
        return
    if level == "info":
        now = time.time()
        if now - _NOTIFY_LOG["ts"] < 1.0:
            return
        _NOTIFY_LOG["ts"] = now
    _notify("notifications/message", {
        "level": level,
        "logger": "stalker-translate",
        "data": text[:1500],
    })


def _open_local_file(path):
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _write_progress_page(path, d, pairs, status="running", elapsed=0.0, note=""):
    """写实时进度页：浏览器每 2 秒刷新。

    总进度 = 已译条目 / 开跑前统计出的可译条目；当前文件单独一根细进度条。
    旧版把「已处理文件累计到的条目数」当分母，会出现 20/53 这种假进度。
    """
    data = dict(d or {})
    files = int(data.get("files") or 0)
    idx = int(data.get("file_idx") or 0)
    phase = str(data.get("phase") or "")
    reqs = int(data.get("reqs") or 0)
    entries_total = int(data.get("entries_total") or 0)
    translatable = int(data.get("entries_translatable") or 0)
    translated = int(data.get("ids") or 0)
    file_name = str(data.get("file") or "")
    file_done = int(data.get("file_done") or 0)
    file_total = int(data.get("file_total") or 0)
    scan_done = int(data.get("prescan_done") or 0)
    scan_total = int(data.get("prescan_total") or 0)

    state_label = {"running": "翻译中", "done": "已完成", "error": "出错",
                   "stopped": "已停止", "paused": "已暂停",
                   "stopping": "正在停止",
                   "needs_review": "待复核"}.get(status, status)

    denom = max(translatable, translated)
    if phase == "prescan" and scan_total > 0:
        pct = int(100.0 * min(scan_done, scan_total) / scan_total)
    elif status == "done":
        pct = 100
    elif denom > 0:
        pct = int(100.0 * translated / denom)
    elif files > 0:
        pct = int(100.0 * min(idx, files) / files)
    else:
        pct = 0
    pct = max(0, min(100, pct))
    file_pct = (int(100.0 * min(file_done, file_total) / file_total)
                if file_total > 0 else 0)

    def _num(n):
        return format(int(n), ",")

    if phase == "prescan":
        overall = "正在统计可翻译条目 · 已扫 %s / %s 张文本表" % (
            _num(scan_done), _num(scan_total))
        overall_num = "%s / %s 张" % (_num(scan_done), _num(scan_total))
        cur_num = "统计中"
        cur_name = "先统计整个 MOD 的条目总量，再开始翻译"
    elif denom > 0:
        overall = "已译 %s / 可译 %s 条" % (_num(translated), _num(denom))
        overall_num = "%s / %s 条" % (_num(translated), _num(denom))
        cur_num = ("%s / %s 条" % (_num(file_done), _num(file_total))
                   if file_total else "—")
        cur_name = file_name or "—"
    else:
        overall = "文件 %s / %s" % (_num(idx), _num(files))
        overall_num = "%s / %s 个" % (_num(idx), _num(files))
        cur_num = ("%s / %s 条" % (_num(file_done), _num(file_total))
                   if file_total else "—")
        cur_name = file_name or "—"

    if elapsed:
        mins, secs = divmod(int(elapsed), 60)
        time_text = ("%d 分 %02d 秒" % (mins, secs)) if mins else ("%d 秒" % secs)
    else:
        time_text = ""

    chips = []
    if files:
        chips.append("文件 %s / %s" % (_num(idx), _num(files)))
    if entries_total:
        chips.append("总条目 %s" % _num(entries_total))
    if translated:
        chips.append("已译 %s" % _num(translated))
    if reqs:
        chips.append("请求 %s" % _num(reqs))
    if time_text:
        chips.append("用时 %s" % time_text)
    chips_html = "".join("<span>%s</span>" % html.escape(c) for c in chips)

    bars = (
        '<div class="bars">'
        '<div class="blk"><div class="lbl"><span>总进度</span>'
        '<span class="num">%s</span></div>'
        '<div class="bar"><i style="width:%d%%"></i></div></div>'
        '<div class="blk"><div class="lbl"><span>当前文件</span>'
        '<span class="num">%s</span></div>'
        '<div class="bar cur"><i style="width:%d%%"></i></div>'
        '<div class="fn">%s</div></div>'
        '</div>'
        % (html.escape(overall_num), pct, html.escape(cur_num), file_pct,
           html.escape(cur_name)))

    rows = []
    for src, dst in list(pairs or ())[-20:][::-1]:
        rows.append('<tr><td class="src">%s</td><td class="dst">%s</td></tr>'
                    % (html.escape(str(src)), html.escape(str(dst))))
    if not rows:
        rows.append('<tr><td colspan="2" class="empty">等待第一批译文……</td></tr>')
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<title>STALKER 汉化进度</title>
<style>
:root{color-scheme:dark}
body{margin:0;padding:16px 20px;background:#12151a;color:#e6e9ef;font:13px/1.55 "Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:1080px}
h1{font-size:16px;font-weight:600;margin:0 0 4px}
h1 .state{color:#3fa9f5;font-weight:500;margin-left:4px}
h1 .pct{color:#8ef0c6;font-size:16px;margin-left:8px}
.meta{color:#c9d4e3;font-size:12px;margin:0 0 8px}
.bar{height:8px;background:#232833;border-radius:4px;overflow:hidden;max-width:560px}
.bar>i{display:block;height:100%%;background:linear-gradient(90deg,#3fa9f5,#38d39f);transition:width .4s}
.bar.cur{max-width:420px;height:6px;margin-bottom:12px}
.row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin:12px 0 4px}
.row .k{color:#9aa4b2;font-size:12px}
.row .v{color:#d7dee8;font-size:12px;word-break:break-all}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 14px}
.stats span{background:#1b2029;border:1px solid #2a3140;border-radius:6px;padding:3px 8px;font-size:12px;color:#c9d4e3}
table{width:100%%;border-collapse:collapse;table-layout:fixed}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #232833;vertical-align:top;font-size:12px;line-height:1.45}
th{color:#9aa4b2;font-weight:500}
td.src{color:#aab4c3;width:48%%;word-break:break-word}
td.dst{color:#8ef0c6;word-break:break-word}
.empty{color:#6b7686;text-align:center}
.note{color:#f0b35b;margin-top:12px;white-space:pre-wrap;font-size:12px}
</style></head><body><div class="wrap">
<h1>STALKER 汉化进度 · <span class="state">%s</span><span class="pct">%d%%</span></h1>
<div class="meta">%s</div>
<div class="bar"><i style="width:%d%%"></i></div>
<div class="stats">%s</div>
%s
<table><thead><tr><th>原文</th><th>译文</th></tr></thead><tbody>%s</tbody></table>
%s
</div></body></html>""" % (
        html.escape(state_label), pct, html.escape(overall), pct,
        chips_html, bars, "".join(rows),
        ('<div class="note">%s</div>' % html.escape(str(note))) if note else "",
    )
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(tmp, path)
    except Exception:
        pass


def _stdin_reader():
    """后台读 stdin，主循环与“服务端发起的请求”（如弹窗）共用同一个队列。"""
    try:
        for line in _PROTO_IN:
            _IN_Q.put(line)
    except Exception:
        pass
    finally:
        _IN_Q.put(None)


def _client_request(method, params, timeout=1800):
    """向客户端发一个 JSON-RPC 请求并等它的响应（如 elicitation/create）。

    只在工具调用线程里使用；期间收到的通知忽略，客户端反向请求回 -32601。
    返回 result dict；超时/客户端报错/连接断开返回 None。
    """
    _REQ_SEQ[0] += 1
    req_id = "srv-%d" % _REQ_SEQ[0]
    _send({"jsonrpc": "2.0", "id": req_id, "method": method,
           "params": params})
    deadline = time.time() + timeout
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            _log("等待客户端响应超时：%s（%.0fs）" % (method, timeout))
            return None
        try:
            raw = _IN_Q.get(timeout=remain)
        except queue.Empty:
            return None
        if raw is None:
            _IN_Q.put(None)
            _log("等待 %s 时 stdin 已关闭" % method)
            return None
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if msg.get("id") == req_id:
            if "error" in msg:
                _log("客户端拒绝 %s：%s" % (method, msg["error"]))
                return None
            return msg.get("result")
        if msg.get("id") is not None and msg.get("method"):
            _send({"jsonrpc": "2.0", "id": msg["id"],
                   "error": {"code": -32601,
                             "message": "服务端不支持该请求"}})
        elif msg.get("method"):
            _log("等待表单时收到通知：%s" % msg.get("method"))


class ToolError(Exception):
    """工具可预期的失败（参数/环境问题），直接回给客户端。"""


# ── 配置与内核 ──────────────────────────────────────────────────────

def _hermes_defaults(cfg):
    """作为 Hermes 插件运行时，自动补齐端点/模型/API Key，做到零配置。"""
    home = os.environ.get("LOCALAPPDATA") or ""
    path = os.path.join(home, "Hermes", "config.yaml")
    if not os.path.isfile(path):
        return cfg
    base = model = ""
    try:
        in_model = False
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                if not in_model:
                    if line.strip() == "model:":
                        in_model = True
                    continue
                if line and not line[0].isspace():
                    break
                stripped = line.strip()
                if stripped.startswith("base_url:"):
                    base = stripped.split(":", 1)[1].strip().strip("\"'")
                elif stripped.startswith("default:"):
                    model = stripped.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        return cfg
    if base and (not cfg.get("base_url")
                 or cfg["base_url"] == "http://127.0.0.1:8080/v1"):
        cfg["base_url"] = base
    if model and (not cfg.get("model") or cfg["model"] == "local-model"):
        cfg["model"] = model
    if not cfg.get("api_key"):
        key_path = os.path.join(home, "hermes", "runtimes", "llamacpp",
                                ".api_key")
        try:
            with open(key_path, "r", encoding="utf-8") as f:
                cfg["api_key"] = f.read().strip()
        except OSError:
            pass
    return cfg


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
    cfg = _hermes_defaults(cfg)
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
        # 暂停/停止控制（由 translate_mod 按任务绑定；None = 不控制）
        self.pause_event = None
        self.stop_event = None
        # 最近翻译的 (原文, 译文)，供工具行进度显示“正在翻什么”。
        self.recent = deque(maxlen=10)

    # 内核只读这些属性；所有请求都真发到远端，所以恒为“运行中”。
    def is_running(self):
        return True

    def stop(self):
        pass

    def _gate(self):
        """暂停时挡住新请求；停止时不再挡（交给 TranslationJob 的 cancel 检查）。"""
        ev = self.pause_event
        st = self.stop_event
        while ev is not None and ev.is_set() and not (st is not None and st.is_set()):
            time.sleep(0.25)

    def translate(self, text, src_lang, tgt_lang, **kw):
        self._gate()
        kw.setdefault("timeout_sec", self.timeout_sec)
        out = self.plugin.LlamaServer.translate(
            self, text, src_lang, tgt_lang, **kw)
        try:
            self.recent.append((str(text)[:240], str(out)[:240]))
        except Exception:
            pass
        return out

    def translate_batch(self, lines, src_lang, tgt_lang, **kw):
        self._gate()
        kw.setdefault("timeout_sec", self.timeout_sec)
        out = self.plugin.LlamaServer.translate_batch(
            self, lines, src_lang, tgt_lang, **kw)
        # 批量结果带 [n] 编号；解析出原文/译文对，供进度页显示“正在翻什么”。
        try:
            srcs = list(lines or [])
            for m in re.finditer(r"\[(\d+)\]\s*([^\n]*)", str(out or "")):
                n = int(m.group(1))
                dst = (m.group(2) or "").strip()
                if 1 <= n <= len(srcs) and dst:
                    self.recent.append((str(srcs[n - 1])[:240], dst[:240]))
        except Exception:
            pass
        return out


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


# ── 并发路数规划（显卡/显存/端点槽位）──────────────────────────────

def _fmt_mb(mib):
    mib = int(mib or 0)
    return ("%.1f GB" % (mib / 1024.0)) if mib >= 1024 else ("%d MB" % mib)


def _gpu_facts(plugin):
    """显卡名 + 显存（MB）。N 卡优先，其余独显取显存最大者；取不到返回 0。"""
    if _GPU_CACHE["done"]:
        return _GPU_CACHE["name"], _GPU_CACHE["vram_mb"]
    name, vram = "", 0
    try:
        nv = plugin.detect_nvidia_gpu()
        if nv:
            name, vram = str(nv[0]), int(nv[1] or 0)
    except Exception:
        pass
    try:
        for gname, gmem in plugin.detect_all_gpus() or []:
            gmem = int(gmem or 0)
            low = str(gname).lower()
            if gmem > vram and not plugin._is_integrated_gpu(low):
                name, vram = str(gname), gmem
    except Exception:
        pass
    _GPU_CACHE.update(done=True, name=name, vram_mb=vram)
    return name, vram


def _parse_llama_args(args):
    """把 llama-server 的启动参数列表解析成 dict（只认 --key value 形式）。"""
    opts = {}
    i = 0
    while i < len(args or []):
        item = str(args[i])
        if item.startswith("--"):
            key = item[2:]
            if i + 1 < len(args) and not str(args[i + 1]).startswith("--"):
                opts[key] = str(args[i + 1])
                i += 2
                continue
            opts[key] = True
        i += 1
    return opts


def _endpoint_facts(cfg):
    """从 /v1/models、/props、/slots 读端点真实槽位、每槽上下文与模型路径。

    Hermes 的 18434 是路由：/v1/models 的 args 里带子进程 --port，需要再向
    子进程查 /props 才能拿到 total_slots / model_path。
    """
    base = _strip_v1(cfg.get("base_url"))
    key = cfg.get("api_key") or ""
    facts = {"base": base, "slots": 0, "ctx": 0, "model_path": "",
             "port": 0, "approx": True}
    try:
        status, data = _http_json(base + "/v1/models", key)
        entries = data.get("data") if isinstance(data, dict) else []
        entry = None
        for ent in entries or []:
            if isinstance(ent, dict) and str(ent.get("id")) == cfg.get("model"):
                entry = ent
                break
        if entry is None:
            for ent in entries or []:
                if (isinstance(ent, dict)
                        and (ent.get("status") or {}).get("value") == "loaded"):
                    entry = ent
                    break
        raw_args = None
        if isinstance(entry, dict):
            status = entry.get("status")
            if isinstance(status, dict) and isinstance(status.get("args"), list):
                raw_args = status["args"]
            elif isinstance(entry.get("args"), list):
                raw_args = entry["args"]
        opts = _parse_llama_args(raw_args)
        port = opts.get("port")
        if str(port or "").isdigit():
            facts["port"] = int(port)
        for field, name in (("ctx-size", "ctx"), ("parallel", "slots")):
            val = opts.get(field)
            if str(val or "").isdigit():
                facts[name] = int(val)
        if isinstance(opts.get("model"), str):
            facts["model_path"] = opts["model"]
    except Exception:
        pass

    # 子进程端口与主端口不同时，直接问子进程（Hermes 的 llama-server 子进程
    # 通常没开鉴权；带 key 也不会被拒绝）。
    host = urllib.parse.urlsplit(base).hostname or "127.0.0.1"
    main_port = urllib.parse.urlsplit(base).port or 80
    props_base = base
    if facts["port"] and facts["port"] != main_port:
        props_base = "http://%s:%d" % (host, facts["port"])
    try:
        status, props = _http_json(props_base + "/props", key, timeout=5)
        if isinstance(props, dict):
            if str(props.get("total_slots") or "").isdigit():
                facts["slots"] = int(props["total_slots"])
            mp = props.get("model_path")
            if (isinstance(mp, str) and mp
                    and mp.lower() not in ("none", "null")
                    and os.path.isfile(mp)):
                facts["model_path"] = mp
            n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
            if str(n_ctx or "").isdigit() and int(n_ctx) > 0:
                facts["ctx"] = int(n_ctx)
    except Exception:
        pass
    try:
        status, slots = _http_json(props_base + "/slots", key, timeout=5)
        if isinstance(slots, list) and slots:
            facts["slots"] = len(slots)
    except Exception:
        pass
    return facts


def _model_path_hint(cfg, model):
    """尽量找到某个模型名的本地 gguf 路径（用于按模型估算显存）。"""
    if not model:
        return ""
    base = _strip_v1(cfg.get("base_url"))
    key = cfg.get("api_key") or ""
    try:
        status, data = _http_json(base + "/v1/models", key, timeout=5)
        for ent in (data.get("data") if isinstance(data, dict) else []) or []:
            if not isinstance(ent, dict) or str(ent.get("id")) != model:
                continue
            args = None
            st = ent.get("status")
            if isinstance(st, dict) and isinstance(st.get("args"), list):
                args = st["args"]
            elif isinstance(ent.get("args"), list):
                args = ent["args"]
            path = _parse_llama_args(args).get("model")
            if isinstance(path, str) and os.path.isfile(path):
                return path
    except Exception:
        pass
    home = os.environ.get("LOCALAPPDATA") or ""
    presets = os.path.join(home, "hermes", "runtimes", "llamacpp", "presets.ini")
    if os.path.isfile(presets):
        try:
            section = ""
            with open(presets, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("[") and s.endswith("]"):
                        section = s[1:-1]
                    elif section == model and "=" in s and s.lower().startswith("model"):
                        path = s.split("=", 1)[1].strip()
                        if os.path.isfile(path):
                            return path
        except Exception:
            pass
    path = os.path.join(home, "hermes", "models", str(model) + ".gguf")
    return path if os.path.isfile(path) else ""


def _translation_model_choices(cfg):
    """端点上的可选翻译模型：优先 Hy-MT2/translate，返回 (names, labels, default)。"""
    models = []
    try:
        models = [str(m) for m in (_probe_endpoint(cfg).get("models") or []) if m]
    except Exception:
        pass
    seen = set()
    models = [m for m in models if not (m in seen or seen.add(m))]
    prefer = [m for m in models
              if any(k in m.lower() for k in ("hy-mt", "hy_mt", "translate"))]
    pool = prefer or models
    if not pool:
        return [], [], cfg.get("model") or ""
    agent = _hermes_agent_model()
    labels = []
    for name in pool:
        low = name.lower()
        if "1.8b" in low:
            labels.append("%s · 快，推荐日常" % name)
        elif "7b" in low:
            labels.append("%s · 质量更好，更慢、更占显存" % name)
        elif agent and name == agent:
            labels.append("%s · Hermes 对话模型，不建议翻译" % name)
        else:
            labels.append(name)
    default = cfg.get("model") if cfg.get("model") in pool else pool[0]
    return pool, labels, default


def _route_plan(cfg):
    """生成 1..N 路选项：显存预算与端点真实槽位取较小值，附每路理由。"""
    plugin = _load_kernel()
    base = _strip_v1(cfg.get("base_url"))
    cache_key = (base, cfg.get("model"), int(cfg.get("ctx_size") or 8192))
    if _PLAN_CACHE["key"] == cache_key and _PLAN_CACHE["plan"]:
        return _PLAN_CACHE["plan"]

    facts = _endpoint_facts(cfg)
    hint_path = _model_path_hint(cfg, cfg.get("model"))
    gpu_name, vram = _gpu_facts(plugin)
    ctx = max(512, int(cfg.get("ctx_size") or 8192))
    model_path = hint_path or str(facts.get("model_path") or "")
    est = {}
    if model_path and os.path.isfile(model_path):
        try:
            est = plugin.estimate_vram_mib(model_path, ctx, 1, gpu_layers=99)
        except Exception:
            est = {}
    weights = int(est.get("weights") or 0)
    kv = int(est.get("kv") or 0)
    compute = int(est.get("compute") or 68)
    approx = not est.get("ok")
    if kv <= 0:
        kv = max(1, int(ctx * 64 * 1024 / (1024 * 1024)))  # 64 KiB/token 经验值
        approx = True
    if weights <= 0 and model_path and os.path.isfile(model_path):
        try:
            weights = os.path.getsize(model_path) // (1024 * 1024)
        except OSError:
            pass

    budget = int(vram * plugin.PARALLEL_GPU_BUDGET) if vram else 0
    if not budget:
        ram = plugin.physical_ram_mb()
        budget = int(ram * plugin.PARALLEL_CPU_BUDGET) if ram else 0
    hw_max = 0
    if budget > 0:
        hw_max = plugin.parallel_max_routes(budget, weights, kv, compute)

    slots = int(facts.get("slots") or 0)
    slots_assumed = False
    if slots <= 0:
        # Hermes 的 llama-server 预设默认 4 槽；模型未加载时查不到 total_slots，
        # 按 4 槽保守估算，绝不按显存上限虚报（否则用户会选到会排队的路数）。
        slots = min(hw_max, 4) if hw_max else 4
        slots_assumed = True
    max_now = min(slots, hw_max) if hw_max else slots
    max_now = max(1, min(plugin.PARALLEL_CAP, max_now))

    options = []
    for n in range(1, max_now + 1):
        used = weights + kv * n + compute
        reason = "预计显存占用 %s" % _fmt_mb(used) if vram else "预计内存占用 %s" % _fmt_mb(used)
        if budget:
            reason += "（预算 %s，余量 %s）" % (_fmt_mb(budget),
                                               _fmt_mb(max(0, budget - used)))
        if n == 1:
            reason += "；单路最稳，首次翻译或显存紧张时选它"
        if n == max_now and max_now > 1:
            reason += "；用满当前可用资源，吞吐最高"
        label = "%d 路（%s）" % (
            n, "推荐" if n == max_now and max_now > 1
            else ("最稳" if n == 1 else "约 " + _fmt_mb(used)))
        options.append({"parallel": n, "label": label, "reason": reason})

    notes = []
    if gpu_name and vram:
        notes.append("显卡：%s（%s 显存）" % (gpu_name, _fmt_mb(vram)))
    elif vram:
        notes.append("显卡：%s 显存" % _fmt_mb(vram))
    else:
        notes.append("未检测到独显，按内存预算估算；CPU 翻译会明显更慢")
    notes.append("模型：%s%s；每路上下文 %d token"
                 % (cfg.get("model") or "?", "（体积未知，按 KV 粗估）" if approx else "",
                    ctx))
    notes.append("端点槽位：%d" % slots)
    if slots_assumed:
        notes.append("端点未报告槽位（模型可能未加载），按默认 4 槽估算")
    if hw_max and slots < hw_max:
        notes.append("硬件估算可开 %d 路，端点当前 %d 槽；要开到 %d 路，"
                     "请在 Hermes 的模型启动参数里加 --parallel %d"
                     % (hw_max, slots, min(hw_max, plugin.PARALLEL_CAP),
                        min(hw_max, plugin.PARALLEL_CAP)))
    elif hw_max and slots > hw_max:
        notes.append("显存预算只够 %d 路，已按显存收窄（端点有 %d 槽）"
                     % (hw_max, slots))
    if approx:
        notes.append("模型体积/KV 结构未能完整读取，显存数字为粗估")

    plan = {
        "gpu": {"name": gpu_name, "vram_mb": vram},
        "model": {"name": cfg.get("model") or "", "path": model_path,
                  "weights_mb": weights, "ctx_per_route": ctx,
                  "approx": approx},
        "endpoint": {"base_url": cfg.get("base_url") or "", "slots": slots,
                     "slots_assumed": slots_assumed},
        "budget_mb": budget,
        "max_by_vram": hw_max,
        "recommended": max_now,
        "options": options,
        "notes": notes,
    }
    _PLAN_CACHE["key"] = cache_key
    _PLAN_CACHE["plan"] = plan
    return plan


def _supports_elicitation():
    caps = _CLIENT.get("caps") or {}
    if "elicitation" in caps:
        return True
    return str(_CLIENT.get("name") or "").lower().startswith("hermes")


def _hermes_agent_model():
    """尽力读取 Hermes config.yaml 的 model.default（用于提示模型混用风险）。"""
    home = os.environ.get("LOCALAPPDATA") or ""
    path = os.path.join(home, "Hermes", "config.yaml")
    if not os.path.isfile(path):
        return ""
    try:
        in_model = False
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                if not in_model:
                    if line.startswith("model:") and line.strip() == "model:":
                        in_model = True
                    continue
                if line and not line[0].isspace():
                    break
                stripped = line.strip()
                if stripped.startswith("default:"):
                    return stripped.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _ask_start_options(plan, cfg):
    """弹窗让用户选翻译模型和并发路数。返回 (路数, 模型, 备注)。"""
    options = plan.get("options") or []
    rec = int(plan.get("recommended") or 1)
    allowed = [int(o["parallel"]) for o in options]
    models, model_labels, default_model = _translation_model_choices(cfg)
    has_parallel = len(options) >= 2
    has_model = len(models) >= 2

    if not _supports_elicitation():
        try:
            n = int(cfg.get("parallel"))
        except (TypeError, ValueError):
            n = 0
        if n not in allowed:
            n = rec
        note = ("当前客户端不支持选择弹窗，已使用 %d 路%s；"
                "可在调用时直接传 parallel / model 覆盖"
                % (n, ("、模型 " + default_model) if default_model else ""))
        return n, default_model, note
    if not has_parallel and not has_model:
        return rec, default_model, ""

    lines = list(plan.get("notes") or [])
    lines.append("")
    props = {}
    required = []
    if has_model:
        lines.append("选择翻译模型：")
        for name, label in zip(models, model_labels):
            lines.append("  · " + label)
        lines.append("（切换模型会让本地端点重新加载，首次可能要等几十秒；"
                     "Hermes 对话模型可能被换出）")
        props["model"] = {
            "type": "string",
            "title": "翻译模型",
            "description": "7B 质量更好但更慢、更占显存；1.8B 更快",
            "enum": models,
            "enumNames": model_labels,
            "default": default_model,
        }
        required.append("model")
    if has_parallel:
        if has_model:
            lines.append("（路数按默认模型估算；选 7B 会自动收窄）")
        lines.append("选择并发路数：")
        for opt in options:
            lines.append("  · %d 路 — %s" % (opt["parallel"], opt["reason"]))
        props["parallel"] = {
            "type": "integer",
            "title": "并发路数",
            "description": "路数越多吞吐越高，显存/上下文占用也越大",
            "enum": [o["parallel"] for o in options],
            "enumNames": [o["label"] for o in options],
            "default": rec,
        }
        required.append("parallel")

    result = _client_request("elicitation/create", {
        "message": "\n".join(lines),
        "requestedSchema": {"type": "object", "properties": props,
                            "required": required},
    })
    if result is None:
        raise ToolError("未收到选择，翻译未启动。可重新发起，或直接指定 "
                        "parallel / model 参数。")
    if str(result.get("action") or "") != "accept":
        raise ToolError("已取消：未选择翻译参数，翻译未启动")
    content = result.get("content") or {}
    chosen_model = default_model
    if has_model:
        value = content.get("model")
        if value in models:
            chosen_model = value
    chosen_parallel = rec
    if has_parallel:
        try:
            chosen_parallel = int(content.get("parallel"))
        except (TypeError, ValueError):
            chosen_parallel = rec
        if chosen_parallel not in allowed:
            chosen_parallel = rec
    return chosen_parallel, chosen_model, ""


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
        plan = _route_plan(cfg)
        out["gpu"] = plan["gpu"]
        out["endpoint_slots"] = plan["endpoint"]["slots"]
        out["routes"] = {
            "recommended": plan["recommended"],
            "max_by_vram": plan["max_by_vram"],
            "options": [o["parallel"] for o in plan["options"]],
            "notes": plan["notes"],
        }
    except Exception as e:
        out["routes_error"] = "%s: %s" % (type(e).__name__, e)
    agent_model = _hermes_agent_model()
    if agent_model:
        out["hermes_agent_model"] = agent_model
        if agent_model == (cfg.get("model") or ""):
            out.setdefault("warnings", []).append(
                "翻译模型与 Hermes agent 模型相同（%s）：翻译模型通常不擅长"
                "工具调用，建议把 agent 换成指令模型" % agent_model)
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
        quality_issues = []
        if out == text and plugin._has_translatable_text(text):
            quality_issues.append("模型未产生可用译文")
        if not plugin.runtime_tokens_match(text, out):
            out = eng.translate(text, src, tgt, glossary_hits=hints or None,
                                extra_sys=plugin._PLACEHOLDER_RETRY_NOTE)
            if not plugin.runtime_tokens_match(text, out):
                quality_issues.append("颜色码/占位符未能原样保留")
        if tgt == "chs":
            role_issues = plugin.semantic_role_issues(text, out, src)
            if role_issues:
                out = eng.translate(text, src, tgt, glossary_hits=hints or None,
                                    extra_sys=plugin._SEMANTIC_ROLE_NOTE)
                role_issues = plugin.semantic_role_issues(text, out, src)
                if role_issues:
                    quality_issues.extend(role_issues)
        if not plugin.runtime_tokens_match(text, out):
            if "颜色码/占位符未能原样保留" not in quality_issues:
                quality_issues.append("颜色码/占位符未能原样保留")
            out = text
        elif quality_issues:
            out = text
    except Exception as e:
        raise ToolError("翻译请求失败：%s: %s。%s"
                        % (type(e).__name__, e, _endpoint_hint(cfg)))
    result = {"translation": out, "source": text, "model": eng.model,
              "term_hits": [[k, v] for k, v in (hints or [])]}
    if quality_issues:
        result["retained_source"] = True
        result["quality_issues"] = quality_issues
    return result


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

    # 模型与路数：显式传入就直接用；否则先弹窗让用户选模型和路数。
    plan = None
    plan_note = ""
    chosen_model = str(args.get("model") or cfg.get("model") or "")
    parallel = args.get("parallel")
    if parallel is None:
        plan = _route_plan(cfg)
        parallel, chosen_model, plan_note = _ask_start_options(plan, cfg)
    else:
        try:
            parallel = int(parallel)
        except (TypeError, ValueError):
            raise ToolError("parallel 必须是 1-16 的整数")
        parallel = max(1, min(16, parallel))
        try:
            plan = _route_plan(cfg)
        except Exception:
            plan = None

    # 选了和默认不同的模型：按该模型重算路数上限，避免 7B 按 1.8B 的预算开太多路
    if chosen_model and chosen_model != (cfg.get("model") or ""):
        try:
            mplan = _route_plan(dict(cfg, model=chosen_model))
            mrec = max(1, int(mplan.get("recommended") or 1))
            if parallel > mrec:
                note = ("所选模型 %s 显存只够 %d 路，已从 %d 路收窄"
                        % (chosen_model, mrec, parallel))
                plan_note = (plan_note + "；" + note) if plan_note else note
                parallel = mrec
        except Exception:
            pass
    if plan:
        slots = int((plan.get("endpoint") or {}).get("slots") or 0)
        if slots and parallel > slots:
            note = ("选择了 %d 路，但端点只有 %d 槽，超出的请求会在服务端排队；"
                    "要真正并行请在模型启动参数里加 --parallel %d"
                    % (parallel, slots, parallel))
            plan_note = (plan_note + "；" + note) if plan_note else note

    eng = _get_engine()
    if chosen_model:
        eng.model = chosen_model
        eng.model_name = chosen_model
    parser = plugin._load_vendor()
    logs = []

    def log(msg, tag="info"):
        line = str(msg)
        logs.append(line)
        if len(logs) > 400:
            del logs[:200]
        _log("[job] " + line)
        if tag in ("err", "warn"):
            _mcp_log("error" if tag == "err" else "warning", line)
        elif (not line.startswith("  ├──")
              and any(k in line for k in ("进度", "文本表：", "引用层完成",
                                          "全部完成", "多路翻译", "会话词表",
                                          "高频专名", "译文目录"))):
            _mcp_log("info", line)

    token = args.get("_progress_token")
    last_emit = {"t": 0.0}
    started_at = time.time()
    progress_page = os.path.join(out_dir, ".progress.html")
    last_state = {"d": {}}

    def progress(d):
        with _PROGRESS_LOCK:
            now = time.time()
            if now - last_emit["t"] < 1.5:
                return
            last_emit["t"] = now
            last_state["d"] = dict(d or {})
            total = int(d.get("files") or 0)
            idx = int(d.get("file_idx") or 0)
            ids = int(d.get("ids") or 0)
            total_ids = int(d.get("entries_translatable")
                            or d.get("total_ids") or 0)
            phase = str(d.get("phase") or "")
            if phase == "prescan":
                msg = "统计可翻译条目 %d/%d 张文本表" % (
                    int(d.get("prescan_done") or 0),
                    int(d.get("prescan_total") or 0))
            else:
                msg = "%d/%d %s" % (idx, total, d.get("file") or "")
                if total_ids:
                    msg += " | 已译 %d / 可译 %d 条" % (ids, total_ids)
            try:
                pairs = list(eng.recent)
            except Exception:
                pairs = []
            pair = pairs[-1] if pairs else None
            if pair:
                msg += " | %s → %s" % (str(pair[0])[:60], str(pair[1])[:60])
            if token:
                _notify("notifications/progress", {
                    "progressToken": token,
                    "progress": idx, "total": total,
                    "message": msg,
                })
            _mcp_log("info", "进度 " + msg)
            try:
                _write_progress_page(progress_page, d, pairs, "running",
                                     elapsed=now - started_at)
            except Exception:
                pass

    merged, user_set, _deleted, _clear = plugin.load_glossary_layers()
    hard = set(plugin.load_hard_glossary_keys()) | set(user_set)

    # 暂停/停止：进度板写控制文件，这里轮询；状态写回 out_dir 供页面显示。
    os.makedirs(out_dir, exist_ok=True)
    try:
        _write_progress_page(progress_page, {"files": 0, "file_idx": 0, "file": ""},
                             [], "running", note="正在扫描 MOD……")
        if bool(cfg.get("open_progress_page", True)):
            _open_local_file(progress_page)
    except Exception:
        pass
    control_path = os.path.join(out_dir, ".ai_translate_control.json")
    status_path = os.path.join(out_dir, ".ai_translate_status.json")
    pause_event = threading.Event()
    stop_event = threading.Event()
    eng.pause_event = pause_event
    eng.stop_event = stop_event

    def _write_status(state):
        try:
            tmp = status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"state": state, "updated": time.time()}, f,
                          ensure_ascii=False)
            os.replace(tmp, status_path)
        except OSError:
            pass

    _write_status("running")
    try:
        os.remove(control_path)
    except OSError:
        pass
    watcher_stop = threading.Event()

    def _watch_control():
        last = 0.0
        while not watcher_stop.is_set():
            try:
                if os.path.isfile(control_path):
                    mt = os.path.getmtime(control_path)
                    if mt != last:
                        last = mt
                        with open(control_path, "r", encoding="utf-8") as f:
                            action = str((json.load(f) or {}).get("action") or "")
                        if action == "pause":
                            pause_event.set()
                            _write_status("paused")
                            _mcp_log("info", "已暂停：不再发送新的翻译请求")
                        elif action == "resume":
                            pause_event.clear()
                            _write_status("running")
                            _mcp_log("info", "已继续翻译")
                        elif action == "stop":
                            stop_event.set()
                            pause_event.clear()
                            _write_status("stopping")
                            _mcp_log("warning", "已请求停止：当前请求结束后停下")
            except Exception:
                pass
            time.sleep(0.3)

    threading.Thread(target=_watch_control, daemon=True).start()

    job = plugin.TranslationJob(
        parser, eng, in_dir, mode, src, tgt,
        glossary=dict(merged),
        force_glossary=True,
        out_dir=out_dir,
        log=log, detail=log, progress=progress,
        cancel_event=stop_event,
        reference_mode=True, copy_reference=True,
        only_referenced=False, protect_proper=True,
        preview_reference=False,
        hard_glossary_keys=hard or None,
        translate_literals=True, auto_session_terms=True,
        tm_lite=True, freq_names=True,
        parallel=parallel)
    t0 = time.time()
    error = ""
    job_ok = False
    try:
        with contextlib.redirect_stdout(sys.stderr):
            job_ok = bool(job.run())
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, e)
        _log("翻译任务异常：\n" + traceback.format_exc())
    elapsed = time.time() - t0
    watcher_stop.set()
    eng.pause_event = None
    eng.stop_event = None
    audit = getattr(job, "audit_result", None)
    failed_strings = int(getattr(job, "failed_strings", 0) or 0)
    audit_bad = bool(audit and not audit.get("ok"))
    if stop_event.is_set():
        status = "stopped"
    elif error:
        status = "error"
    elif not job_ok or failed_strings or audit_bad:
        status = "needs_review"
    else:
        status = "done"
    _write_status(status)
    try:
        _write_progress_page(progress_page, last_state["d"], list(eng.recent),
                             status, elapsed=elapsed, note=error)
    except Exception:
        pass
    warns = [x for x in logs if "错误" in x or "失败" in x or "警告" in x
             or "⚠" in x]
    result = {
        "status": status,
        "input_dir": in_dir,
        "output_dir": job.out_dir,
        "progress_page": progress_page,
        "elapsed_sec": round(elapsed, 1),
        "requests": int(getattr(job, "req_count", 0) or 0),
        "translated_chars": int(getattr(job, "trans_chars", 0) or 0),
        "failed_strings": failed_strings,
        "audit": audit,
        "files": (job.batch_stats or {}).get("batches"),
        "model": eng.model,
        "parallel": parallel,
        "log_tail": logs[-30:],
    }
    if plan:
        result["route"] = {
            "gpu": plan["gpu"],
            "slots": plan["endpoint"]["slots"],
            "recommended": plan["recommended"],
            "max_by_vram": plan["max_by_vram"],
        }
    if plan_note:
        result["route_note"] = plan_note
    if error:
        result["error"] = error
        result["hint"] = _endpoint_hint(cfg)
    if status == "stopped":
        result["hint"] = ("已停止。重新运行同一命令会从断点继续，"
                          "已完成文件会自动跳过。")
    if status == "needs_review":
        result["hint"] = ("处理已结束，但终检或翻译失败统计未通过；"
                          "请查看 audit 和 log_tail，修正后重新运行续译。")
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
                        "写出可用的汉化补丁。用户给出 MOD 目录后直接调用本工具，"
                        "不要先用 terminal/文件工具逐文件扫描，也不要要求用户上传"
                        "文件——本工具会自行识别整个目录。省略 parallel 时会先弹窗："
                        "自动检测显卡/显存与端点槽位，列出可用的翻译模型（1.8B/7B）"
                        "和 1..N 路及理由让用户选择。"
                        "结果写入 output_dir（默认输入目录下的 translated_hy_mt2），"
                        "可重复运行续译。只有 status=done 表示通过终检；"
                        "needs_review 表示已有输出但存在硬性问题，"
                        "必须报告待复核并查看 audit/log_tail。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input_dir": {"type": "string",
                              "description": "MOD 根目录的绝对路径（例如 "
                                             "C:\\Users\\Admin\\Desktop\\IWP\\gamedata）"},
                "output_dir": {"type": "string",
                               "description": "输出目录；省略则用 "
                                              "input_dir/translated_hy_mt2"},
                "parallel": {"type": "integer", "minimum": 1, "maximum": 16,
                             "description": "并发路数；省略则自动检测硬件并弹窗"
                                            "让用户选择"},
                "model": {"type": "string",
                          "description": "翻译模型名；省略则弹窗选择，或使用 "
                                         "config.json 中的默认模型"},
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
        _CLIENT["caps"] = params.get("capabilities") or {}
        info = params.get("clientInfo") or {}
        _CLIENT["name"] = str(info.get("name") or "")
        _ok(msg_id, {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": ("STALKER 汉化翻译。用户要汉化 MOD 时直接调用 "
                             "translate_mod（input_dir=用户给的目录）；不要先用 "
                             "terminal/execute_code 扫描目录。parallel 省略会弹窗让"
                             "用户选择翻译模型和并发路数。只有 status=done 才能报告"
                             "翻译完成；needs_review 必须报告待复核并引用 audit/log_tail。"
                             "只有翻译报错时才调用 check_env。"
                             "只翻译一段文本用 translate_text。本机是 Windows，"
                             "路径形如 C:\\Users\\...，不要使用 /Users/ 或 /home/。"),
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
    threading.Thread(target=_stdin_reader, daemon=True).start()
    while True:
        try:
            raw = _IN_Q.get()
        except KeyboardInterrupt:
            break
        if raw is None:
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
