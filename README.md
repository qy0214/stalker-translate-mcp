# STALKER MOD 汉化 MCP 插件

把 STALKER / X-Ray MOD 批量译成简体中文。项目只交付 **Hermes MCP 插件包**，
不提供独立 CLI；翻译模型由 Hermes 或用户自己的 OpenAI 兼容端点提供。

## 安装

1. 解压 `stalker-translate-MCP-1.1.9.zip`。
2. 双击 `install-plugin.cmd`。
3. 安装器会复制插件、启用工具、安装技能并尝试重启 Hermes 内核。
4. 看到“安装成功”后重启或重新打开 Hermes，直接说“汉化 D:\mods\你的MOD”。

安装器只依赖 Windows 自带 PowerShell 5.1，不需要单独安装 Python，也不需要管理员权限。
如果安装时以管理员身份运行，请用普通方式重新打开 Hermes，避免内核权限与桌面端不一致。

## 功能

- `check_env`：检查模型端点、术语库、X-Ray 解析器、显卡与并发路数。
- `translate_mod`：汉化整个 MOD；首次调用可同时选择 1.8B/7B 模型和并发路数。
- `translate_text`：翻译一小段文本。
- 实时进度页、暂停/继续/停止、MCP 日志通知、断点续译。
- 输出默认写入 `<MOD目录>\translated_hy_mt2`，不覆盖原文件。
- 选择 7B 时按显存重新收窄并发路数，降低显存不足风险。

## 模型与配置

插件不内置模型。运行时优先读取 Hermes 的本地模型端点、默认模型和 API Key；
也可以编辑插件目录中的 `config.json`：

```json
{
  "base_url": "http://127.0.0.1:8080/v1",
  "api_key": "",
  "model": "Hy-MT2-1.8B-Q8_0",
  "ctx_size": 8192,
  "parallel": 1,
  "open_progress_page": true,
  "timeout_sec": 300
}
```

本机常用端点是 `http://127.0.0.1:18434/v1`。日常整 MOD 推荐
`Hy-MT2-1.8B-Q8_0`，`Hy-MT2-7B-Q4_K_M` 语感更好但更慢、更占显存。

## 源码结构

```text
hermes-plugin/                 1.1.9 主线源码
  server.py                    MCP stdio 服务
  kernel/                      翻译内核、术语库、X-Ray 解析器
  runtime/                     便携 Python
  skills/stalker-translate/    Hermes 技能
  install-plugin.ps1           双击安装器
tests/smoke_test.py            1.1.9 MCP 全流程测试
package.ps1                    生成分享包
```

## 开发与打包

```powershell
python tests/smoke_test.py
python -m py_compile hermes-plugin/server.py hermes-plugin/kernel/plugin.py
powershell -ExecutionPolicy Bypass -File .\package.ps1 -OutDir "C:\Users\Admin\Desktop"
```

进度记录见 [docs/progress/STATUS.md](docs/progress/STATUS.md)。
翻译内核来自
[stalker-localization-toolkit-ai-translate-2x](https://github.com/qy0214/stalker-localization-toolkit-ai-translate-2x)
（GPL-3.0）。
