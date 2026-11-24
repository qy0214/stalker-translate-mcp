# STALKER 汉化 MCP 服务

## Hermes Agent

1. 解压分享包，双击 `install-plugin.cmd`。
2. 安装器会自动找到 Hermes 目录、复制插件、启用插件，并重启 Hermes 内核。
3. 看到“安装成功：Hermes 已自动加载 stalker-translate 翻译工具”即可使用。
   如果 Hermes 窗口提示未连接，关闭窗口再重新打开。

安装器会同时关闭 Hermes 的工具折叠（`config.yaml` 中的
`tools.tool_search.enabled: "off"`），让 `check_env`、`translate_mod`、
`translate_text` 直接出现在小模型的工具列表里；并把同名技能安装到
`<HermesHome>\skills\stalker-translate`，小模型可以直接按 `stalker-translate`
加载，不需要加插件前缀。插件本身不再注册同名技能，Hermes 里只会看到一个
`stalker-translate`。

为避免小模型放着翻译工具不用、跑去开终端/浏览器/写代码，安装器还会：

- 在本 profile 的 `agent.disabled_toolsets` 中关闭 `terminal`、`file`、
  `browser`、`code_execution`、`computer_use`；
- 在 `<HermesHome>\SOUL.md` 写入一段带 `<!-- stalker-translate:begin -->`
  标记的常驻规则（Hermes 会自动加载进系统提示词）。

需要恢复这些工具时，删除 `config.yaml` 中 `agent.disabled_toolsets` 里对应的
行，以及 `SOUL.md` 中标记内的内容即可。

如果安装器是以管理员身份运行的，它不会自动启动 Hermes 内核（管理员权限的内核
会让普通权限的 Hermes 桌面端提示连接失败），请用普通方式重新打开 Hermes。

不需要安装 Python、模型或其它平台；MCP 服务会自动读取 Hermes 的本地
模型端点（`config.yaml` 的 `model.base_url` 与 API Key）。

## 翻译模型与路数

首次调用 `translate_mod` 会弹窗同时选择：

- **翻译模型**：`Hy-MT2-1.8B-Q8_0`（快，推荐日常）或
  `Hy-MT2-7B-Q4_K_M`（质量更好，更慢、更占显存）；
- **并发路数**：按显卡/显存和端点槽位给出 1..N 路及理由。

选 7B 时会按 7B 的显存重新收窄路数。切换模型会让本地端点重新加载，
首次可能要等几十秒，Hermes 对话模型也可能被暂时换出。

`config.json` 的 `model` 是默认模型（当前 `Hy-MT2-1.8B-Q8_0`）；
调用 `translate_mod` 时也可以显式传 `model` / `parallel` 跳过弹窗。

## 进度与译文

`translate_mod` 运行时会在输出目录生成实时进度页：

```text
<MOD目录>\translated_hy_mt2\.progress.html
```

页面每 2 秒自动刷新，显示文件进度、条目数、请求数，以及最近 20 条
“原文 → 译文”。默认在翻译开始时自动用浏览器打开；不想自动打开，可在
`config.json` 里设置 `"open_progress_page": false`。

Hermes 的工具行同时会显示 `文件 x/y | 条目 a/b | 原文 → 译文`。

## 完成判定与续译

- `status: done`：翻译失败数为 0，且交付终检通过。
- `status: needs_review`：已有输出，但终检或翻译失败统计未通过；
  查看返回结果中的 `audit`、`failed_strings` 和 `log_tail`。
- 终检发现硬性问题的文件会撤销完成记录，但不会删除已生成输出；
  重新运行同一命令会重译这些问题文件。
- 旧版账本缺少管线版本标记，升级到 1.1.10 后会自动重译一次；
  之后使用带版本标记的新账本正常续译。
- 无实际译文变化的文件不再输出，也不计入完成账本。
- 中断时会补齐已写引用所需的生成字符串定义，避免留下悬空 ID。

终检覆盖悬空 ID、零变化输出、颜色码/占位符损坏、整条英文/俄文漏译、
上下文串入、可确定性识别的人称/付款方向冲突和硬术语缺失。语义门禁不能
替代实际 MOD 的真机翻译质量复核。

手动兜底：把本目录复制到 `<HermesHome>\plugins\stalker-translate`，在
`config.yaml` 的 `plugins.enabled` 中加入 `stalker-translate`，然后完全退出
Hermes（包括系统托盘）再重新打开。

## 其它 MCP 平台

把本目录作为标准 stdio MCP 服务：

- `command`: `<本目录>\runtime\python.exe`
- `args`: `<本目录>\server.py`

然后编辑 `config.json`，填写平台的 OpenAI 兼容端点：

```json
{
  "base_url": "http://127.0.0.1:8080/v1",
  "model": "你的模型名",
  "api_key": ""
}
```

## 说明

- 术语库、筛选/保护规则、X-Ray 解析器都在 `kernel/` 内，随包携带。
- 本包**不包含模型**；模型由平台/教程部署。
- 翻译结果默认写到 `<MOD目录>/translated_hy_mt2`，重复运行会续译。
