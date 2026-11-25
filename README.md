> **【已归档 · ARCHIVED】本仓库已并入五合一单仓库（monorepo），新地址：**
> **https://github.com/qy0214/stalker-localization-toolkit-ai-translate-2x**
>
> Hermes MCP 插件现由上库 `products/mcp_hermes/` 统一维护与发布（当前 1.1.10，
> 与本库最后版本一致）；共享解析核 `core/stalker_translate_core` 亦在上库单源管理。
> 本仓库自即日起只读归档，不再接受 issue / PR / 新版本发布；新功能、缺陷修复
> 与新版本请一律到上述单仓库。

# STALKER MOD 汉化 MCP 插件

把 STALKER / X-Ray MOD 批量译成简体中文。项目只交付 **Hermes MCP 插件包**，
不提供独立 CLI；翻译模型由 Hermes 或用户自己的 OpenAI 兼容端点提供。

## 安装

1. 解压 `stalker-translate-MCP-1.1.10.zip`。
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
- 交付前强制终检：悬空生成 ID、零变化输出、占位符损坏、整条漏译、
  上下文串入、语义方向冲突和硬术语缺失都会阻止任务标记为完成。
- 无实际译文变化的文件不再生成，也不再计入完成账本。
- 失败任务返回 `needs_review`，并撤销问题文件的完成记录，保留输出供复核和重译。
- 旧版完成账本缺少管线版本标记，升级后会自动重译一次，避免跳过既有问题文件。

`translate_mod` 的 `status` 为 `done` 才表示通过离线终检；`needs_review`
表示已有输出但存在硬性问题，应查看 `audit` 和 `log_tail`。`translate_text`
无法安全产出时会保留原文，并通过 `retained_source` 和 `quality_issues` 明示。
语义方向门禁只覆盖可确定性识别的人称和交接反转，不能替代真机翻译质量复核。

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

## 版本迭代

| 版本 | 日期 | 简要说明 |
|---|---|---|
| 1.0.0 | 2026-11-24 | 独立仓库交付，注册 Hermes，首个 MCP 冒烟测试通过。 |
| 1.1.1 | 2026-11-24 | 增加实时进度板、暂停/继续/停止、断点续译和日志通知。 |
| 1.1.2 | 2026-11-24 | 安装后自动重启 Hermes，修复 YAML 的 UTF-8 读写问题。 |
| 1.1.5 | 2026-11-24 | 改为单技能安装，禁用危险工具集，加入 `SOUL.md` 常驻规则。 |
| 1.1.6 | 2026-11-24 | 修复管理员权限启动内核导致 Hermes 桌面端连接失败的问题。 |
| 1.1.7 | 2026-11-24 | 增加工具行进度、浏览器实时进度页和 MCP 日志通知。 |
| 1.1.8 | 2026-11-24 | 首次调用时同时弹出 1.8B/7B 模型与并发路数选择。 |
| 1.1.9 | 2026-11-25 | 修复进度页回归，将测试迁移到主线，清理旧 CLI 支线并重新打包验证。 |
| **1.1.10** | **2026-11-25** | 修复中断悬空 ID、零变化输出、占位符损坏、整条漏译和完成状态误报；增加语义方向与硬术语终检、问题文件重译及旧账本迁移。 |

历史记录中未形成独立交付的 `1.1.3`、`1.1.4` 不在上表列出。
最新分享包和校验信息见 [Releases](https://github.com/qy0214/stalker-translate-mcp/releases)。

## 源码结构

```text
hermes-plugin/                 1.1.10 主线源码
  server.py                    MCP stdio 服务
  kernel/                      翻译内核、术语库、X-Ray 解析器
  runtime/                     便携 Python
  skills/stalker-translate/    Hermes 技能
  install-plugin.ps1           双击安装器
tests/smoke_test.py            1.1.10 MCP 全流程测试
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
