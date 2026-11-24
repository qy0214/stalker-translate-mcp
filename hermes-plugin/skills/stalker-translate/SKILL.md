---
name: stalker-translate
version: 1.1.10
description: "汉化 STALKER / X-Ray 的 MOD：把 MOD 目录里的文本 XML、cfgxml、ltx、脚本批量译成简体中文，自动套用内置术语库（阵营/地点/变异生物/物品/人物）。用户说“翻译这个 MOD”“汉化这个目录”“把这段 STALKER 文本译成中文”时使用。"
license: GPL-3.0
---

# STALKER MOD 汉化

## 何时使用

- 用户给出 STALKER / X-Ray MOD 目录，要求汉化。
- 用户贴来一段游戏文本，要求译成中文。
- 普通文本不要使用本技能。

## Hermes 工具全名

- 环境检查：`mcp__stalker_translate_plugin__check_env`
- 整目录汉化：`mcp__stalker_translate_plugin__translate_mod`
- 单段文本：`mcp__stalker_translate_plugin__translate_text`

工具列表里也可能显示为 `check_env`、`translate_mod`、`translate_text`。
如果工具被折叠，用 `tool_describe` / `tool_call` 调用上面的全名。

## 流程（照做，不要自由发挥）

1. 用户消息里通常已经带 MOD 路径（例如 `C:\Users\Admin\Desktop\IWP`），直接使用它。
2. 立即调用 `translate_mod`，只传 `input_dir`；省略 `parallel`，工具弹窗会让你选择翻译模型（1.8B/7B）和并发路数。
3. 工具运行期间不要重复调用，等待结果。
4. 结束后先看 `status`：只有 `done` 才能报告“翻译完成”；`needs_review` 必须
   报告“待复核”，并说明 `failed_strings`、`audit` 和 `log_tail` 中的问题。
5. 正常完成时汇报：输出目录、文件数、失败数、实际路数、实际使用的翻译模型。

## 硬性禁止

- 不要用 `terminal`、`execute_code` 或文件搜索工具逐文件扫描 MOD；`translate_mod` 会自己识别。
- 不要启动、下载或切换模型；模型端点由 Hermes 管理。
- 不要猜测路径。本机是 Windows，路径形如 `C:\Users\<用户>\...`，不是 `/Users/...` 或 `/home/...`。
- 不要要求用户上传文件。
- 只有翻译报错时才调用 `check_env`。
