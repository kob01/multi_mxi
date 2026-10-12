---
trigger: glob
glob:
  - app/docs/**
  - app/bodies/**
  - app/rag/ingest.py
  - scripts/migrate_doc_stores.py
---

# 文档解析类版本号：NORMALIZER_VERSION 与 PARSER_VERSION 同升降

改动 `app/docs/normalize.py::normalize_text()` 的**任何**行为——空白压缩、零宽字符剥离、NFKC 约定、换行处理——必须同步递增 `docker/.env` 里的 `NORMALIZER_VERSION`（`n1` → `n2` …）。

## 原因

`doc_parents.start_offset` / `end_offset` 与 Mongo `parent_texts` 正文都以 `normalized_text` 为基准。归一化规则一改，全库旧 offset 集体失效，切回来的片段不再是原文的连续子串，表现为引用定位/高亮错乱、表格问答答错列——**但不报错**。版本号存于 `doc_bodies.normalizer_version` / `doc_parents.normalizer_version`，校验脚本据此判定重入库清单。

## 配套约束

- 只加读取逻辑、不改 `normalize_text()` 输出时**不要**递增版本：版本一变会触发全库重入库判定。
- 改切块策略（父块/子块长度、`PARENT_CHUNK_MAX` 等）同样需要重入库，走 `scripts/migrate_doc_stores.py`，不要手改库。
- 本文件覆盖到的 `app/bodies/store.py`、`app/rag/ingest.py` 里有按 offset 回填正文的热路径；在其中新增按 offset 取片段的代码时，先确认当前 `normalizer_version` 与数据一致。
- Excel 合并单元格与裸值切块的历史修复（表格问答断行/错列）不得回退。

# PARSER_VERSION 必须与解析/切块行为同升降

改动 `app/docs/parsers.py`（各模态解析、OCR 调用口径）、`app/rag/ingest.py` 的切块策略
（`CHUNK_SIZE`/`CHUNK_OVERLAP`/`split_chunks`/`parent_chunk_max`/`build_parent_child`）或
`app/docs/normalize.py` 的归一化行为时，必须同步递增 `PARSER_VERSION`（`p1` → `p2` …），
两轨 dotenv（`.env` 与 `docker/.env`）同名同值。

## 原因

解析产物 `MongoDB parse_artifacts` 的**复用判据是 `content_sha256 + parser_version`**：
同一份字节只解析一次，预览与入库读的都是那一份。解析/切块行为改了而不递增版本号，
worker 会继续复用旧产物，表现为「改了解析代码没反应」——而且不报错，答案质量静默变差。

## 配套约束

- 只新增读路径/不改解析与切块输出时**不要**递增：版本一变会让所有未过期产物重解析（一批
  扫描 PDF 就是几分钟级 OCR）。
- `PARSER_VERSION` 与 `NORMALIZER_VERSION` 是两个独立轴：前者管「块怎么来」，后者管
  「offset 以什么为基准」。只改归一化时递增后者即可，但两者都变时必须都递增。
- 已入库数据的 offset 一致性判定仍看 `doc_parents.normalizer_version`，不看 parser_version。
