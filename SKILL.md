---
name: stalker-translate
version: 1.0.0
description: "汉化 STALKER / X-Ray 的 MOD：把 MOD 目录里的文本 XML、cfgxml、ltx、脚本批量译成简体中文，自动套用内置术语库（阵营/地点/变异生物/物品/人物）。用户说“翻译这个 MOD”“汉化这个目录”“把这段 STALKER 文本译成中文”时使用。"
author: qy0214
license: GPL-3.0
metadata:
  hermes:
    tags: [stalker, xray, translation, localization, mod, chinese]
    related_skills: []
---

# STALKER MOD 汉化（本地模型）

## When to Use

- 用户给出一个 STALKER / X-Ray MOD 目录，要求翻译或汉化。
- 用户贴来一段游戏文本（对话、任务、物品说明），要求译成中文。
- 用户问“汉化环境是否就绪”“为什么翻译失败”。

不要用它翻译普通非游戏文本；也不要启动、下载或切换模型——模型由用户按
自己的教程在 Hermes 里部署，本技能只负责翻译。

## 工具

| 工具 | 用途 |
|---|---|
| `check_env` | 检查模型端点、术语库、解析器。首次使用或报错时先调用；`deep=true` 会真发一句测试。 |
| `translate_mod` | 汉化整个 MOD 目录，输出可用的汉化补丁。 |
| `translate_text` | 翻译一小段文本。 |

## 标准流程

1. **确认目录**：向用户确认 MOD 根目录（通常包含 `configs`、`gamedata`
   或 `text` 子目录）。不要自行猜测路径。
2. **检查环境**：调用 `check_env`。若 `ready=false`，把 `problems` 原样
   转述给用户，并提示“请先按教程在 Hermes 中启动本地模型”，不要反复重试。
3. **开始汉化**：调用 `translate_mod`，只传 `input_dir`。默认输出到
   `<MOD目录>/translated_hy_mt2`，与源目录分离，不会覆盖原文件。
4. **汇报结果**：说明输出目录、耗时、请求数、失败条目数；若 `warnings`
   非空，挑 1–3 条最重要的转述。失败条目 >0 时建议再跑一次（会续译，
   已完成的文件自动跳过）。

## 参数

- `translate_mod(input_dir, output_dir?, mode?, source_lang?)`
  - `mode="auto"`（默认）覆盖文本 XML / cfgxml / ltx / 脚本；`text` 只译文本 XML。
  - `source_lang="auto"` 自动识别俄语/英语/乌克兰语。
- `translate_text(text, context?, source_lang?, target_lang?)`
  - `context` 只帮助理解，不会被翻译。

## 注意

- **不要覆盖原 MOD**：输出目录必须与输入目录不同；工具已默认隔离。
- **可重复运行**：中断后再次调用同一命令即可续译（完成账本按文件指纹判断）。
- **长任务**：大 MOD 可能跑很久，进度会以通知形式出现；不要中途重复发起
  同一目录的任务。
- **模型端点**：`config.json` 的 `base_url` 指向 Hermes 的本地模型端点
  （安装脚本会自动填好）。端点不可达时，唯一正确的动作是让用户启动模型，
  而不是改模型名或反复重试。
- **术语库**：随技能内置，无需用户配置；用户自定义词条放在
  `kernel/assets/glossary_user.json` 会被自动合并。
