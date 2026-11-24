#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 2.x 主仓库同步翻译内核到本技能。

用法：
    python tools/sync_kernel.py
    python tools/sync_kernel.py --from "D:\\path\\to\\STALKER-AiTranslate-2x\\plugins\\ai_translate"

同步内容：plugin.py + assets（术语库/内置词库/X-Ray 解析器/第三方声明）。
复制后自动重新应用唯一的内核修改：推理请求的 model 字段可配置。
"""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FROM = os.path.join(os.path.dirname(HERE), "STALKER-AiTranslate-2x",
                            "plugins", "ai_translate")
ASSETS = ("stalker_termbase.json", "glossary_builtin.json",
          "xray_pkg.zip", "THIRD_PARTY_NOTICES.txt")

OLD = '"model": "hy-mt2",'
NEW = ('# MCP 适配：端点可能要求具体模型名；插件内置引擎仍用默认 hy-mt2。\n'
       '            "model": getattr(self, "model_name", "hy-mt2"),')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=DEFAULT_FROM,
                    help="2.x 插件的 plugins/ai_translate 目录")
    args = ap.parse_args()
    src = os.path.abspath(args.src)
    if not os.path.isfile(os.path.join(src, "plugin.py")):
        sys.exit("找不到源内核：%s" % os.path.join(src, "plugin.py"))

    dst_root = os.path.join(HERE, "kernel")
    dst_assets = os.path.join(dst_root, "assets")
    os.makedirs(dst_assets, exist_ok=True)

    shutil.copy2(os.path.join(src, "plugin.py"),
                 os.path.join(dst_root, "plugin.py"))
    missing = []
    for name in ASSETS:
        s = os.path.join(src, "assets", name)
        if not os.path.isfile(s):
            missing.append(name)
            continue
        shutil.copy2(s, os.path.join(dst_assets, name))
    if missing:
        sys.exit("源目录缺少资源：%s" % "、".join(missing))

    path = os.path.join(dst_root, "plugin.py")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if OLD in text:
        text = text.replace(OLD, NEW)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("已应用 model 字段补丁（%d 处）" % NEW.count("model_name"))
    elif "model_name" in text:
        print("model 字段补丁已存在，跳过")
    else:
        sys.exit("未找到预期的 model 字段，请检查上游 plugin.py 是否改了实现")

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if '"version":' in line and "2.0." in line:
                print("内核版本：%s" % line.strip().rstrip(","))
                break
    print("同步完成：%s" % dst_root)


if __name__ == "__main__":
    main()
