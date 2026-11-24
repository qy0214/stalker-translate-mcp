# -*- coding: utf-8 -*-
"""MCP 冒烟测试：mock OpenAI 端点 + 真实 stdio JSON-RPC 全流程。

运行：python tests/smoke_test.py
覆盖：initialize / tools/list / check_env / translate_text / translate_mod
"""

import contextlib
import importlib.util
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
PLUGIN_ROOT = os.path.join(ROOT, "hermes-plugin")
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
            [sys.executable, server_path or os.path.join(PLUGIN_ROOT, "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.err, cwd=PLUGIN_ROOT, env=env)
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self._id = 0
        self.elicitations = []
        self.server_logs = []

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
            if (resp.get("method") == "elicitation/create"
                    and resp.get("id") is not None):
                self.elicitations.append(resp)
                props = (resp.get("params", {}).get("requestedSchema", {})
                         .get("properties", {}))
                content = {}
                if (props.get("model", {}).get("enum")):
                    content["model"] = props["model"]["enum"][0]
                if (props.get("parallel", {}).get("enum")):
                    content["parallel"] = props["parallel"]["enum"][0]
                self._write({"jsonrpc": "2.0", "id": resp["id"],
                             "result": {"action": "accept",
                                        "content": content}})
                continue
            if resp.get("method") == "notifications/message":
                self.server_logs.append(resp.get("params") or {})
                continue
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


def load_kernel():
    path = os.path.join(PLUGIN_ROOT, "kernel", "plugin.py")
    spec = importlib.util.spec_from_file_location("stalker_test_kernel", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(mod)
    return mod


def kernel_regressions(tmp):
    plugin = load_kernel()
    parser = plugin._load_vendor()

    class NoServer(object):
        def translate(self, *args, **kwargs):
            raise AssertionError("无翻译内容的文件不应请求模型")

        def translate_batch(self, *args, **kwargs):
            raise AssertionError("无翻译内容的文件不应请求模型")

    symbols_in = os.path.join(tmp, "symbols", "st_symbols.xml")
    os.makedirs(os.path.dirname(symbols_in), exist_ok=True)
    with open(symbols_in, "w", encoding="utf-8") as f:
        f.write('<string_table><string id="sym"><text>12345</text>'
                '</string></string_table>')
    symbols_out_dir = os.path.join(tmp, "symbols_out")
    symbols_out = os.path.join(symbols_out_dir, "st_symbols.xml")
    os.makedirs(symbols_out_dir, exist_ok=True)
    with open(symbols_out, "w", encoding="utf-8") as f:
        f.write('<string_table><string id="sym"><text>12345</text>'
                '</string></string_table>')
    job = plugin.TranslationJob(
        parser, NoServer(), os.path.dirname(symbols_in), "text", "auto", "chs",
        glossary={}, out_dir=symbols_out_dir, force_glossary=False,
        freq_names=False, tm_lite=False)
    job._ledger["st_symbols.xml"] = {"out_size": 1}
    st = job._translate_table(symbols_in, symbols_out, "st_symbols.xml")
    check("零变化符号表不输出且不记账",
          st.get("skipped_file") and not os.path.exists(symbols_out)
          and "st_symbols.xml" not in job._ledger, st)

    migration_in = os.path.join(tmp, "migration", "st_old.xml")
    migration_out = os.path.join(tmp, "migration_out", "st_old.xml")
    os.makedirs(os.path.dirname(migration_in), exist_ok=True)
    os.makedirs(os.path.dirname(migration_out), exist_ok=True)
    with open(migration_in, "w", encoding="utf-8") as f:
        f.write('<string_table><string id="old"><eng>Hello</eng>'
                '</string></string_table>')
    with open(migration_out, "w", encoding="utf-8") as f:
        f.write('<string_table><string id="old"><text>你好</text>'
                '</string></string_table>')
    migration = plugin.TranslationJob(
        parser, NoServer(), os.path.dirname(migration_in), "text", "eng", "chs",
        glossary={}, out_dir=os.path.dirname(migration_out),
        force_glossary=False, freq_names=False, tm_lite=False)
    migration._ledger_present = True
    migration._ledger["st_old.xml"] = {
        "in_size": os.path.getsize(migration_in),
        "in_mtime_ns": os.stat(migration_in).st_mtime_ns,
        "out_size": os.path.getsize(migration_out),
    }
    check("旧版完成账本触发重译",
          not migration._should_skip("st_old.xml", migration_in,
                                     migration_out, "text"))
    migration._record_done("st_old.xml", migration_in, migration_out)
    check("新版完成账本记录管线版本",
          migration._ledger["st_old.xml"].get("pipeline")
          == plugin.PLUGIN_INFO["version"], migration._ledger)

    check("运行期 token 计数校验",
          plugin.runtime_tokens_match("%c[default]%s $clr_2", "%s%c[default]$clr_2")
          and not plugin.runtime_tokens_match("%c[default]Hello", "%c[默认]汉译"))
    check("任务语义方向反转检测",
          bool(plugin.semantic_role_issues("I will pay you.", "你必须付给我。", "eng"))
          and bool(plugin.semantic_role_issues("Ты должен заплатить мне.",
                                               "我会付给你。", "rus")))

    token_job = plugin.TranslationJob(
        parser, NoServer(), os.path.dirname(symbols_in), "text", "eng", "chs",
        glossary={}, out_dir=symbols_out_dir, force_glossary=False,
        freq_names=False, tm_lite=False)
    token_job._translate_one_unchecked = (
        lambda *args, **kwargs: "%c[默认]汉译")
    token_src = "%c[default]Hello"
    token_out = token_job._translate_one(token_src, "eng")
    check("损坏颜色码的译文回退原文并计失败",
          token_out == token_src and token_job.failed_strings == 1,
          token_job.failed_strings)

    audit_in = os.path.join(tmp, "audit_in")
    audit_out = os.path.join(tmp, "audit_out")
    os.makedirs(os.path.join(audit_in, "configs", "text", "eng"), exist_ok=True)
    os.makedirs(os.path.join(audit_out, "configs", "text", "eng"), exist_ok=True)
    src = os.path.join(audit_in, "configs", "text", "eng", "st_bad.xml")
    dst = os.path.join(audit_out, "configs", "text", "eng", "st_bad.xml")
    with open(src, "w", encoding="utf-8") as f:
        f.write('<string_table>'
                '<string id="color"><eng>%c[default]Hello</eng></string>'
                '<string id="leak_text"><eng>Hello stalker</eng></string>'
                '</string_table>')
    with open(dst, "w", encoding="utf-8") as f:
        f.write('<string_table>'
                '<string id="color"><eng>%c[默认]汉译</eng></string>'
                '<string id="leak_text"><eng>Hello stalker</eng></string>'
                '</string_table>')
    with open(os.path.join(audit_out, "refs.ltx"), "w", encoding="utf-8") as f:
        f.write("[refs]\nname = sgtat_gen_text_1_1\n")
    audit_job = plugin.TranslationJob(
        parser, NoServer(), audit_in, "text", "eng", "chs",
        glossary={}, out_dir=audit_out, force_glossary=False,
        freq_names=False, tm_lite=False)
    audit_job._audit_tables.add("configs/text/eng/st_bad.xml")
    audit_job._ledger["configs/text/eng/st_bad.xml"] = {"out_size": 1}
    audit_job._ledger["refs.ltx"] = {"out_size": 1}
    audit = audit_job._final_audit(audit_out)
    counts = audit.get("counts") or {}
    check("终检发现颜色码损坏、英文漏译和悬空 ID",
          not audit.get("ok")
          and counts.get("runtime_token_damage", 0) >= 1
          and counts.get("untranslated_entries", 0) >= 1
          and counts.get("dangling_generated_ids", 0) >= 1, counts)
    check("终检失败文件撤销完成记录以便续译",
          not audit_job._ledger
          and os.path.exists(dst) and os.path.exists(
              os.path.join(audit_out, "refs.ltx")), audit_job._ledger)

    cancel_in = os.path.join(tmp, "cancel_in")
    cancel_out = os.path.join(tmp, "cancel_out")
    os.makedirs(cancel_in)
    for name in ("a.ltx", "b.ltx"):
        with open(os.path.join(cancel_in, name), "w", encoding="utf-8") as f:
            f.write("[x]\nname = original\n")
    cancel = threading.Event()
    cancel_job = plugin.TranslationJob(
        parser, NoServer(), cancel_in, "auto", "eng", "chs",
        glossary={}, out_dir=cancel_out, force_glossary=False,
        cancel_event=cancel, freq_names=False, tm_lite=False)
    cancel_job._ledger_saved = time.time()

    def fake_process(full, reuse, extract, prefix, gen):
        gid = prefix + str(gen[0])
        gen[0] += 1
        extract[gid] = "汉译"
        cancel.set()
        return "name = %s\n" % gid, {gid: "汉译"}, "utf-8"

    cancel_job._process_non_text = fake_process
    cancel_ok = cancel_job._run_auto(cancel_out, {})
    cancel_audit = cancel_job._final_audit(cancel_out)
    check("取消时生成 ID 定义已补齐且无悬空引用",
          not cancel_ok and cancel_audit.get("ok")
          and cancel_audit["counts"].get("dangling_generated_ids") == 0,
          cancel_audit.get("counts"))


def main():
    tmp = tempfile.mkdtemp(prefix="stalker_mcp_smoke_")
    try:
        kernel_regressions(tmp)
    except Exception as e:
        check("内核回归测试未抛异常", False, "%s: %s" % (type(e).__name__, e))
    mock = MockOpenAI("Hy-MT2-1.8B-Q8_0", [
        "Hy-MT2-1.8B-Q8_0", "Hy-MT2-7B-Q4_K_M"]).start()
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "base_url": mock.base_url,
            "model": "Hy-MT2-1.8B-Q8_0",
            "ctx_size": 4096,
            "parallel": 1,
            "open_progress_page": False,
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
    with open(os.path.join(mod_dir, "st_symbols.xml"), "w",
              encoding="utf-8") as f:
        f.write('<string_table><string id="st_symbols"><rus>12345</rus>'
                '</string></string_table>')
    env = dict(os.environ)
    env["STALKER_MCP_CONFIG"] = cfg_path
    env["STALKER_MCP_STDERR"] = os.path.join(tmp, "server.stderr.log")
    env["PYTHONUNBUFFERED"] = "1"

    client = McpClient(env, os.environ.get("STALKER_MCP_SERVER"))
    try:
        init = client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"elicitation": {}},
            "clientInfo": {"name": "hermes-agent", "version": "1"},
        })
        check("initialize 返回 serverInfo",
              (init.get("result") or {}).get("serverInfo", {}).get("name")
              == "stalker-translate", init.get("result"))
        check("initialize 版本 1.1.10",
              (init.get("result") or {}).get("serverInfo", {}).get("version")
              == "1.1.10", init.get("result"))
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
        check("translate_mod 弹窗选择路数",
              bool(client.elicitations)
              and client.elicitations[0].get("params", {}).get(
                  "requestedSchema", {}).get("properties", {}).get(
                      "parallel", {}).get("enum"),
              [e.get("params", {}).get("message", "")[:80]
               for e in client.elicitations])
        check("translate_mod 弹窗选择模型",
              bool(client.elicitations)
              and len(client.elicitations[0].get("params", {}).get(
                  "requestedSchema", {}).get("properties", {}).get(
                      "model", {}).get("enum", [])) >= 2,
              [e.get("params", {}).get("message", "")[:120]
               for e in client.elicitations])
        check("translate_mod 正常结束", mod.get("status") == "done", mod)
        check("translate_mod 使用所选路数", mod.get("parallel") == 1,
              mod.get("parallel"))
        check("translate_mod 发送 MCP 日志通知",
              bool(client.server_logs), client.server_logs[:2])
        out_xml = os.path.join(tmp, "mod", "translated_hy_mt2", "text",
                               "st_hello.xml")
        check("输出文件已生成", os.path.isfile(out_xml), out_xml)
        symbols_xml = os.path.join(tmp, "mod", "translated_hy_mt2", "text",
                                   "st_symbols.xml")
        ledger_path = os.path.join(tmp, "mod", "translated_hy_mt2",
                                   ".ai_translate_done.json")
        with open(ledger_path, encoding="utf-8") as f:
            ledger = json.load(f)
        check("零变化文件未生成且未记入完成账本",
              not os.path.exists(symbols_xml)
              and "text/st_symbols.xml" not in ledger,
              [k for k in ledger if "symbols" in k])
        check("正常任务返回通过的终检结果",
              (mod.get("audit") or {}).get("ok") is True, mod.get("audit"))
        check("实时进度页已生成",
              os.path.isfile(os.path.join(tmp, "mod", "translated_hy_mt2",
                                          ".progress.html")),
              os.path.join(tmp, "mod", "translated_hy_mt2", ".progress.html"))
        body = ""
        if os.path.isfile(out_xml):
            with open(out_xml, encoding="utf-8-sig") as f:
                body = f.read()
        check("输出含中文译文", "汉译" in body, body[:160])
        check("translate_mod 耗时合理（<120s）", elapsed < 120,
              "%.1fs" % elapsed)

        token_text = "%c[default]Hello"
        direct = result_json(client.request(
            "tools/call",
            {"name": "translate_text",
             "arguments": {"text": token_text, "source_lang": "eng"}}))
        check("translate_text 拒绝损坏颜色码并保留原文",
              direct.get("translation") == token_text
              and direct.get("retained_source") is True
              and direct.get("quality_issues"), direct)

        bad_mod_dir = os.path.join(tmp, "bad_mod", "text")
        os.makedirs(bad_mod_dir)
        with open(os.path.join(bad_mod_dir, "st_color.xml"), "w",
                  encoding="utf-8") as f:
            f.write('<string_table><string id="color"><eng>%c[default]Hello'
                    '</eng></string></string_table>')
        bad_mod = result_json(client.request(
            "tools/call",
            {"name": "translate_mod",
             "arguments": {"input_dir": os.path.join(tmp, "bad_mod"),
                           "model": "Hy-MT2-1.8B-Q8_0", "parallel": 1}},
            timeout=600))
        check("硬性翻译失败返回 needs_review 而非 done",
              bad_mod.get("status") == "needs_review"
              and bad_mod.get("failed_strings", 0) >= 1
              and bad_mod.get("hint"), bad_mod)
        bad_status_path = os.path.join(tmp, "bad_mod", "translated_hy_mt2",
                                       ".ai_translate_status.json")
        with open(bad_status_path, encoding="utf-8") as f:
            bad_status = json.load(f)
        check("状态文件同步 needs_review",
              bad_status.get("state") == "needs_review", bad_status)
        bad_page_path = os.path.join(tmp, "bad_mod", "translated_hy_mt2",
                                     ".progress.html")
        with open(bad_page_path, encoding="utf-8") as f:
            bad_page = f.read()
        check("进度页显示待复核", "待复核" in bad_page)
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
