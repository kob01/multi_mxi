---
trigger: model_decision
description: 需要定位代码入口、跨模块改动、规划任务拆分，或不熟悉某功能落在哪个文件/目录时，先查仓根 REPO_MAP.md 代码地图（全仓文件清单 + 每文件关键函数/类签名）。
---

# 仓库代码地图：REPO_MAP.md

仓根 `REPO_MAP.md` 是自动生成的代码地图（约 1850 行）：先列出全部纳入地图的文件路径，再对有代码的文件给出关键符号骨架（函数/类签名，`⋮` 表示上下文被省略）。由 aider RepoMap 按 token 预算挑选，所以热点符号优先出现。

## 什么时候用

- **规划任何跨模块任务前先查它**：本仓库有 app/agents、app/assistant、app/rag、app/memory、app/kg、app/docs、app/bodies、app/security、app/db、app/cache 十余个子系统，直接 grep 全仓成本高。
- 已知模块名但不知道具体文件时，或要判断某处改动会牵动哪些文件时。

## 怎么用

1. 用关键词在 `REPO_MAP.md` 里检索（Grep 而非整读，文件较大），拿到候选文件路径。
2. 只对候选文件读源码。**地图是索引不是权威**：签名可能已过期，落地前必须以源文件为准。

## 维护

- 地图是产物，不要手工编辑；结构性改动后重新生成：
  `uv run --no-project --with aider-chat python scripts/gen_repo_map.py --map-tokens 16384 -o REPO_MAP.md`
- 生成脚本用 git 列举文件，所以 `.venv`/`data`/`uploads`/`reports` 不会污染地图；`--all-files` 才退化为裸遍历。
- 另有 `.qoder/repowiki/zh/content/` 存放人工撰写的架构文档（项目总体架构、RAG 知识底座），讲"为什么"；`REPO_MAP.md` 讲"在哪"。两者互补。
