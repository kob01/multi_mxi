---
trigger: glob
glob:
  - app/docs/**
  - app/bodies/**
  - app/rag/ingest.py
  - scripts/migrate_doc_stores.py
---

# NORMALIZER_VERSION 必须与 normalize_text 同升降

改动 `app/docs/normalize.py::normalize_text()` 的**任何**行为——空白压缩、零宽字符剥离、NFKC 约定、换行处理——必须同步递增 `docker/.env` 里的 `NORMALIZER_VERSION`（`n1` → `n2` …）。

## 原因

`doc_parents.start_offset` / `end_offset` 与 Mongo `parent_texts` 正文都以 `normalized_text` 为基准。归一化规则一改，全库旧 offset 集体失效，切回来的片段不再是原文的连续子串，表现为引用定位/高亮错乱、表格问答答错列——**但不报错**。版本号存于 `doc_bodies.normalizer_version` / `doc_parents.normalizer_version`，校验脚本据此判定重入库清单。

## 配套约束

- 只加读取逻辑、不改 `normalize_text()` 输出时**不要**递增版本：版本一变会触发全库重入库判定。
- 改切块策略（父块/子块长度、`PARENT_CHUNK_MAX` 等）同样需要重入库，走 `scripts/migrate_doc_stores.py`，不要手改库。
- 本文件覆盖到的 `app/bodies/store.py`、`app/rag/ingest.py` 里有按 offset 回填正文的热路径；在其中新增按 offset 取片段的代码时，先确认当前 `normalizer_version` 与数据一致。
- Excel 合并单元格与裸值切块的历史修复（表格问答断行/错列）不得回退。
