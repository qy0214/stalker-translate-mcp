# -*- coding: utf-8 -*-
"""MCP 冒烟测试：mock OpenAI 端点 + 真实 stdio JSON-RPC 全流程。

运行：python tests/smoke_test.py
覆盖：initialize / tools/list / check_env / translate_text / translate_mod
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from mock_openai import MockOpenAI  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name,
                       ("  |  " + str(detail)) if detail else ""))


class McpClient(object):
    def __init__(self, env, server_path=None):
        self.log_path = env["STALKER_MCP_STDERR"]
        self.err = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, server_path or os.path.join(ROOT, "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.err, cwd=ROOT, env=env)
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self._id = 0

    def _reader(self):
        for line in self.proc.stdout:
            self.q.put(line)

    def _write(self, obj):
        self.proc.stdin.write(
            (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method, params=None, timeout=300):
        self._id += 1
        msg_id = self._id
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError("等待 %s 响应超时（%ds）" % (method, timeout))
            item = self.q.get(timeout=remain)
            resp = json.loads(item.decode("utf-8"))
            if resp.get("id") == msg_id:
                return resp

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self.err.close()
        except Exception:
            pass


def result_json(resp):
    if "error" in resp:
        raise AssertionError("协议错误：%s" % resp["error"])
    res = resp.get("result") or {}
    if res.get("isError"):
        raise AssertionError("工具报错：%s" % res)
    return json.loads(res["content"][0]["text"])


def main():
    tmp = tempfile.mkdtemp(prefix="stalker_mcp_smoke_")
    mock = MockOpenAI("mock-model").start()
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "base_url": mock.base_url,
            "model": "mock-model",
            "ctx_size": 4096,
            "parallel": 1,
            "timeout_sec": 60,
        }, f)
    mod_dir = os.path.join(tmp, "mod", "text")
    os.makedirs(mod_dir)
    with open(os.path.join(mod_dir, "st_hello.xml"), "w",
              encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<string_table>\n'
                '  <string id="st_hello"><rus>Привет, сталкер. '
                'Как дела?</rus></string>\n'
                '  <string id="st_trader"><rus>Сидорович ждёт тебя '
                'на Кордоне.</rus></string>\n'
                '</string_table>\n')
    env = dict(os.environ)
    env["STALKER_MCP_CONFIG"] = cfg_path
    env["STALKER_MCP_STDERR"] = os.path.join(tmp, "server.stderr.log")
    env["PYTHONUNBUFFERED"] = "1"

    client = McpClient(env, os.environ.get("STALKER_MCP_SERVER"))
    try:
        init = client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1"},
        })
        check("initialize 返回 serverInfo",
              (init.get("result") or {}).get("serverInfo", {}).get("name")
              == "stalker-translate", init.get("result"))
        client.notify("notifications/initialized")

        tools = client.request("tools/list")
        names = [t["name"] for t in (tools.get("result") or {}).get("tools", [])]
        check("tools/list 三个工具",
              names == ["check_env", "translate_mod", "translate_text"], names)

        env_info = result_json(client.request(
            "tools/call", {"name": "check_env", "arguments": {}}))
        check("check_env ready", env_info.get("ready") is True,
              env_info.get("problems"))
        check("check_env 术语库已加载",
              (env_info.get("termbase") or {}).get("canon", 0) > 1000,
              env_info.get("termbase"))

        text = result_json(client.request(
            "tools/call",
            {"name": "translate_text",
             "arguments": {"text": "Привет, сталкер."}}))
        check("translate_text 返回中文译文",
              bool(re.search(r"[\u4e00-\u9fff]", text.get("translation", ""))),
              text)

        t0 = time.time()
        mod = result_json(client.request(
            "tools/call",
            {"name": "translate_mod",
             "arguments": {"input_dir": os.path.join(tmp, "mod")}},
            timeout=600))
        elapsed = time.time() - t0
        check("translate_mod 正常结束", mod.get("status") == "done", mod)
        out_xml = os.path.join(tmp, "mod", "translated_hy_mt2", "text",
                               "st_hello.xml")
        check("输出文件已生成", os.path.isfile(out_xml), out_xml)
        body = ""
        if os.path.isfile(out_xml):
            with open(out_xml, encoding="utf-8-sig") as f:
                body = f.read()
        check("输出含中文译文", "汉译" in body, body[:160])
        check("translate_mod 耗时合理（<120s）", elapsed < 120,
              "%.1fs" % elapsed)
    except Exception as e:
        check("冒烟测试未抛异常", False, "%s: %s" % (type(e).__name__, e))
        print("\n--- server stderr ---")
        try:
            with open(env["STALKER_MCP_STDERR"], encoding="utf-8",
                      errors="ignore") as f:
                print(f.read()[-4000:])
        except Exception:
            pass
    finally:
        client.close()
        mock.stop()
        keep = os.environ.get("STALKER_MCP_KEEP_TMP")
        if keep:
            print("临时目录保留：%s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d 通过" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
