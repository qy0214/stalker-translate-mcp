# STALKER 汉化 MCP 技能（Hermes Agent）

把 STALKER / X-Ray MOD 的文本批量译成简体中文，给 **Hermes Agent** 当技能用。
只提供翻译大脑（术语库 + 提示词 + 翻译管线 + 文件处理），**不集成模型**：
推理请求发给你自己的 OpenAI 兼容端点（Hermes 部署的本地模型）。

## 安装

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

脚本会：

1. 复制技能到 `%LOCALAPPDATA%\Hermes\skills\stalker-translate`
2. 从 Hermes 的 `config.yaml` 读出本地模型端点，生成 `config.json`
3. 把 MCP 服务注册进 `config.yaml`（先备份）

可重复运行；已有 `config.json` 不会被覆盖。

## 使用

重启 Hermes，按教程启动本地模型，然后直接说：

```text
汉化 D:\mods\你的MOD
```

汉化结果默认写到 `<MOD目录>\translated_hy_mt2`，不会覆盖原文件；
重复运行会续译（已完成的文件自动跳过）。

## 工具

| 工具 | 说明 |
|---|---|
| `check_env` | 检查模型端点、术语库、解析器 |
| `translate_mod` | 汉化整个 MOD 目录 |
| `translate_text` | 翻译一小段文本 |

## 配置

`%LOCALAPPDATA%\Hermes\skills\stalker-translate\config.json`：

| 键 | 默认 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:18434/v1` | OpenAI 兼容端点（安装时自动读取） |
| `model` | Hermes 当前模型 | 端点上的模型名；llama.cpp 可忽略 |
| `api_key` | 自动读取 | Hermes 本地端点需要鉴权时，安装脚本从 `runtimes\llamacpp\.api_key` 自动填入 |
| `ctx_size` | 8192 | 每条文本的上下文预算 |
| `parallel` | 1 | 并发请求数（对应 llama-server 的 `--parallel`） |
| `timeout_sec` | 300 | 单次请求超时 |
| `prompt_extra` | 空 | 追加到系统提示词的要求 |

## 目录

```text
SKILL.md               Hermes 技能说明
server.py              MCP 服务（零第三方依赖）
install.ps1            一键安装
config.example.json    配置模板
kernel/                翻译内核（plugin.py + 术语库 + X-Ray 解析器）
tools/sync_kernel.py   从 2.x 主仓库同步内核
tests/                 冒烟测试（内置 mock 模型端点）
```

## 开发

```powershell
python .\tests\smoke_test.py
```

会启动一个本地 mock 模型端点，走完 `initialize → tools/list →
check_env → translate_text → translate_mod` 全流程。

内核来自
[stalker-localization-toolkit-ai-translate-2x](https://github.com/qy0214/stalker-localization-toolkit-ai-translate-2x)
（GPL-3.0），同步方式见 `tools/sync_kernel.py`。
