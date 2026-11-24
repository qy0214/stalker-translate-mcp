# 项目记忆（MEMORY）

> 最后更新：2026-11-25
> 详细进度见 [`docs/progress/STATUS.md`](docs/progress/STATUS.md)。

## 当前硬结论

1. **唯一主线是 `hermes-plugin/`，版本 1.1.9**；根目录旧 CLI、独立安装器、
   重复内核和 1.1.1 测试已清理。
2. **桌面 1.1.9 分享包已生成并验证**：
   `C:\Users\Admin\Desktop\stalker-translate-MCP-1.1.9.zip`。
   包内源码与仓库一致，解压后冒烟测试 15/15 通过。
3. **本机 Hermes 仍安装 1.1.8**；1.1.9 真机安装与翻译验证由用户完成。
4. **1.1.9 已按逻辑整理进 Git，本次未推送远端。**

## 1.1.9 修复与整理

- 修复进度页把 `bars` 误写成 `file_block`，导致页面渲染异常终止翻译。
- 进度回调增加隔离，页面写入失败不再影响翻译主流程。
- 默认 `base_url` 改回 8080，让服务端自动读取 Hermes 实际端点与 API Key。
- 技能版本同步为 1.1.9。
- 冒烟测试切换到 `hermes-plugin/`，覆盖版本、三工具、模型/路数弹窗、
  翻译结果和实时进度页；进度页另做浏览器渲染验证，控制台无错误。
- 打包脚本显式使用 UTF-8，兼容 Windows PowerShell 5.1。

## 用户硬性偏好

- 只做 Hermes 等本地模型平台的 MCP 分享包，**不做独立 CLI**。
- 禁止让低能力模型执行安装，只提供双击安装脚本。
- 分享包不放给模型看的安装命令，不含用户自定义词库、模型文件和运行日志。
- 包内自带便携 Python。
- MCP 真机测试由用户自行完成，助手负责离线验证和可安装交付。

## 关键路径

```text
hermes-plugin/                 1.1.9 主线源码
hermes-plugin/install-plugin.cmd 用户安装入口
tests/smoke_test.py            1.1.9 全流程测试
package.ps1                    分享包构建入口
docs/progress/STATUS.md        权威进度索引
C:\STALKER-AiTranslate-MCP-backup-20261124\  废弃支线与旧 dist 备份
```

## 下一步

1. 用户双击桌面 1.1.9 包安装，重启 Hermes，验证三个工具、单技能、
   1.8B/7B 与路数弹窗、实时进度和实际 MOD 翻译。
2. 真机验证通过后，再由用户决定是否推送 Git 或发布新版本。
