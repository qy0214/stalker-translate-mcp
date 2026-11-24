# -*- coding: utf-8 -*-
"""最小 OpenAI 兼容 mock 端点，供冒烟测试使用。

支持：
  GET  /v1/models
  GET  /health
  POST /v1/chat/completions   （返回“汉译”，保留源文行数）
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _translate(user_text):
    idx = user_text.rfind("：\n")
    payload = user_text[idx + 2:] if idx >= 0 else user_text
    lines = [x for x in payload.splitlines() if x.strip()] or ["文本"]
    out = []
    for i, src in enumerate(lines, 1):
        digits = "".join(re.findall(r"\d+", src))
        out.append("汉译%d%s" % (i, digits))
    return "\n".join(out)


class _Handler(BaseHTTPRequestHandler):
    model = "mock-model"
    models = ["mock-model"]

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": [
                {"id": name, "object": "model"} for name in self.models]})
        elif self.path.startswith("/props"):
            self._json(200, {
                "total_slots": 4, "model_path": "",
                "default_generation_settings": {"n_ctx": 8192},
            })
        elif self.path.startswith("/slots"):
            self._json(200, [{}, {}, {}, {}])
        elif self.path.startswith("/health"):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:
            req = {}
        msgs = req.get("messages") or []
        user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                user = str(m.get("content") or "")
                break
        text = _translate(user)
        self._json(200, {
            "id": "mock-1", "object": "chat.completion",
            "created": 0, "model": req.get("model") or self.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        })


class MockOpenAI(object):
    def __init__(self, model="mock-model", models=None):
        handler = type("Handler", (_Handler,), {
            "model": model,
            "models": list(models or [model]),
        })
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d/v1" % (host, port)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
