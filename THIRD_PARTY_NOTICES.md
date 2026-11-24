# 第三方组件与来源声明

本仓库的 `kernel/` 目录来自 STALKER 本地化工具集 2.x 的 AI 翻译插件，
用于在 Hermes Agent 中复用同一套翻译管线与术语库。

## 1. 本项目主仓库

- 仓库：<https://github.com/qy0214/stalker-localization-toolkit-ai-translate-2x>
- 许可证：GPL-3.0
- 同步的基线版本：`kernel/plugin.py` 的 `PLUGIN_INFO["version"]`（2.0.28）
- MCP 派生改动：推理请求的 `model` 字段改为可配置
  （`getattr(self, "model_name", "hy-mt2")`），并增加 Hermes 所需的零变化文件清理、
  生成字符串 ID 中断落盘、运行期占位符保真、任务语义方向门禁和硬性交付终检。

## 2. wzyddg/stalker_auto-trans-tool_remake — X-Ray 解析器

- 仓库：<https://github.com/wzyddg/stalker_auto-trans-tool_remake>
- 作者：wzyddg
- 许可证：上游仓库未声明开源许可证（2026-10-29 通过 GitHub API 检查为
  `license: null`）。相关代码版权归原作者；本仓库仅保留来源与作者信息。
- 引用方式：`kernel/assets/xray_pkg.zip` 内的 X-Ray 解析器；其中
  `xmlxReader.py` 已由主仓库改为标准库解析并加入健壮性修复。

## 3. 术语库

- `kernel/assets/stalker_termbase.json`、`kernel/assets/glossary_builtin.json`
  由本项目整理维护，随主仓库以 GPL-3.0 分发。

> 如果你是上述代码的权利人，希望调整署名方式或移除相关代码，请开 issue
> 说明，我们会配合处理。
