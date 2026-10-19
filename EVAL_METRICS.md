# 财务 Agent / HR Agent / RAG 评测指标体系

> 范围：`app/agents/finance_agent`、`app/agents/hr_agent`、`app/rag`（含入库与检索全链路），以及决定这两个 Agent 能否被正确触发的路由层。
> 本文只列「评什么、怎么判、判到第几级」，不含实现排期。所有参数值均取自当前代码，非业界通用值。

---

## 一、先立靶子：三个被测对象的真实形态

评测体系必须贴着实际架构长，否则测的是想象中的系统。三者的实际形态差异极大：

| 对象 | 实际编排 | 对评测的含义 |
| --- | --- | --- |
| finance_agent | Plan-Execute-Reflect 三节点闭环（`executor.py:455-467`），`finance_agentic_enabled` 默认 True，关则退 `_legacy_invoke` 单轮 ReAct | 必须**分轨评测**：agentic 轨测规划/反思/重规划，legacy 轨只测终答；两轨质量差本身是一个指标 |
| hr_agent | 纯单轮 `create_agent(llm, tools, system_prompt)`（`executor.py:143-147`），temperature=0.1，**无显式循环上限**，只靠外层 `a2a_timeout=120s` 兜底 | 没有中间产物可断言，只能测「工具选择正确性 + 终答 + 安全」；同时**缺 recursion_limit 本身是一项待测风险** |
| RAG | 入库六态状态机 + 双通道召回 + RRF + 精排 + 父子块展开 | 唯一已有评测基建的对象，但现有评测**绕开了最容易出错的三段**（见 §2.F） |

一条真实请求的漏斗（评测分层就按这个切）：

```
用户原话
  └─ rewrite_query 指代消解
      └─ 意图三层漏斗：规则(13条正则,conf=0.9) → bge-m3语义(top3余弦均值≥0.62 且 margin≥0.05) → LLM → 关键词兜底(0.55/0.4)
          ├─ KNOWLEDGE_QA  → kb_retrieve（RAG 全链路）→ retrieve-judge 环 → 生成/改写重检(≤1)/拒答
          ├─ TOOL_CALL     → 网关自起小 ReAct 直用 MCP 工具（走 Tool Cache，只读前缀 query_/list_/get_/lookup_，TTL 30s）
          ├─ AGENT_DELEGATE→ A2A → finance-agent:9002 / hr-agent:9001
          └─ 显式点选      → multi_agent_execute（跳过漏斗，layer=explicit；Semaphore=2，逐项 150s）
```

**关键结论：路由错则全错。** finance/hr 的内部指标只在「路由正确」这一条件下才有意义，所以路由层必须是 L0，而不是附在最后。

指标编号规则：`层-环节-序号`。优先级 P0=回归即红（上线门禁），P1=每版必测，P2=诊断/观测型。

---

## 二、RAG 评测指标

### A. 入库与解析（文档 → 块）

这一段现有评测**覆盖率为零**（`msmarco_eval` 用每 passage「1 父 1 子」的合成结构，`harness.py:171-201`），而它恰好是历史故障最集中的地方（Excel 合并单元格错列、PDF 无文本层、归一化改动导致 offset 集体失效）。

| 编号 | 指标 | 口径定义 | 判定方式 | 优先级 |
| --- | --- | --- | --- | --- |
| ING-PARSE-01 | 解析成功率 | 按文档类型分桶（pdf/docx/xlsx/pptx/md/txt/图片/srt），`parsing` 阶段无异常且产出 ≥1 父块的比例 | 状态机阶段计数 | P0 |
| ING-PARSE-02 | 表格列对齐正确率 | xlsx 合并单元格锚点回填（`parsers.py:271-279`）后，「列名: 值」还原的列归属与源表一致的行占比 | 人工标注金表 + 逐格 diff | P0 |
| ING-PARSE-03 | 表格断行率 | 按行贪心装桶（`_TABLE_PARENT_BUDGET=1200`）时，单条逻辑记录被切到两个父块的次数 / 总记录数 | 结构断言 | P0 |
| ING-PARSE-04 | PDF 文本层缺失识别率 | 无文本层页面被正确判为需走 MinerU OCR 的比例；OCR 页与文本页混合文档的分派正确率 | 构造样本集 | P1 |
| ING-PARSE-05 | OCR 质量 | 扫描页 OCR 的字符错误率 CER / 词错误率 WER，与人工转写对照 | 金样本对照 | P1 |
| ING-PARSE-06 | 标题层级切分正确率 | md 按 `#` 栈、docx 按 Heading 样式切块后，块边界与真实章节边界的重合度（IoU of span） | 与人工章节标注比 | P1 |
| ING-PARSE-07 | 归一化无损率 | `normalize.py` 六步（CRLF/NFKC/零宽/行尾空白/`\n{3,}`/末单`\n`）后，去噪不改变语义字符；统计被删字符种类分布 | 前后 diff 审计 | P2 |
| ING-OFF-01 | offset 定位成功率 | `_Locator` 单调游标回算 `start_offset/end_offset` 成功、未记 warning 的比例（`ingest.py:192-197`） | 日志计数 | P0 |
| ING-OFF-02 | offset 可回溯率 | 按 offset 从 `normalized_text` 切出的片段，确为该父块正文的连续子串（抽样验证） | 字符串包含断言 | P0 |
| ING-OFF-03 | 版本一致性违规数 | `doc_parents.normalizer_version` 与当前 `NORMALIZER_VERSION`(n1) 不一致的存量行数；`parser_version`(p1) 同理 | SQL 计数 | P0 |
| ING-CHUNK-01 | 超限块占比 | 子块 > `CHUNK_MAX_CHARS=20000` 未截断仅记 warning 的块数（`ingest.py:39-43,186`） | 日志/SQL 计数 | P1 |
| ING-CHUNK-02 | 父块预算利用率 | 父块长度分布相对 `parent_chunk_max=1200` 的分位数；`len<=1200` 时父块即单块（不再滑窗）的文档占比 | 分布统计 | P2 |
| ING-CHUNK-03 | 切块信息自足率 | 抽样块脱离上下文仍可理解的比例（需 LLM/人工判，指代是否悬空、是否以「如下图所示」开头） | LLM-as-judge + 人工抽检 | P1 |
| ING-CHUNK-04 | 重叠冗余率 | `CHUNK_SIZE=512/OVERLAP=64` 造成的重复字符量占语料总字符比例 | 计算 | P2 |
| ING-EMBED-01 | 向量写入完整率 | 应 embed 的子块中实际拿到 1024 维向量的比例；`content_hash` 变化才重 embed 的增量命中率（`pipeline.py:514-543`） | SQL 计数 | P0 |
| ING-EMBED-02 | 向量维度/模型一致性 | 库内不存在非 1024 维或非 `bge-m3` 产出的向量 | 约束校验 | P0 |
| ING-PUB-01 | 发布事务原子性 | 「元数据+tags+父子块+标陈旧+outbox」五写同事务（`pipeline.py:578-634`）失败时零残留 | 故障注入 | P0 |
| ING-PUB-02 | 差集正确率 | `publish_in_session` 只比 `deprecated_at IS NULL` 现行行做增删差（`vectorstore.py:197-254`），误删/漏建块数 | 与全量重建结果比对 | P0 |
| ING-PUB-03 | 陈旧泄漏率 | 已 `deprecated_at` 的块仍被检索命中的次数（应为 0，全读路径门禁） | 检索探针 | P0 |
| ING-PUB-04 | 重入库复位正确率 | upsert 后 `deprecated_at` 已重置为 NULL 的比例（`vectorstore.py:60-66`） | SQL 断言 | P0 |
| ING-GC-01 | GC 一致性 | 宽限 30min / 批 2000 行回收后，PG 已删行的 Mongo 正文是否按 `prune_stale_bodies` 同步收缩；孤儿正文与缺失正文各自计数 | 对账脚本 | P1 |
| ING-GC-02 | 正文不变量违规数 | 「PG 可见行必能取到正文」违反条数（应为 0） | 全库对账 | P0 |
| ING-STATE-01 | 状态机卡死数 | 停留在 `pending/parsing/embedding/publishing/indexing` 超过阈值的文档数（失败不回退，`pipeline.py:57-66`） | SQL 计数 | P0 |
| ING-IDEM-01 | 重放幂等率 | 同一消息重放 N 次后块数/向量数不变 | 重放测试 | P1 |
| ING-DUP-01 | 同内容重复入库率 | 同 `content_sha256` 重复解析未复用产物的次数 | 计数 | P2 |

### B. 检索（query → 上下文）

现有 `msmarco_eval` 已覆盖 IR 六指标，但**评测口径与生产口径不一致**：评测用 `top_k=50/top_n=10/threshold=0`（`__main__.py:45-51`），生产是 `rag_top_k=8 / rerank_top_n=4 / threshold=0.4`。所以「评测分数好」不等于生产质量好，必须补生产口径轨。

| 编号 | 指标 | 口径定义 | 判定方式 | 优先级 |
| --- | --- | --- | --- | --- |
| RET-IR-01 | Hit@k / Recall@k / Precision@k / MRR@k / nDCG@k / MAP@k | 已有实现（`metrics.py:16-106`），二值相关、按 `doc_id` 匹配、漏评不补、`ks=(1,3,5,10)` | **需改造**：新增生产口径轨 k=(3,5,8) + top_n=4 + threshold=0.4 | P0 |
| RET-IR-02 | 通道贡献度 | 最终 top_n=4 中，仅 dense 命中 / 仅 BM25 命中 / 双通道共现 的条数占比 | 拆通道重跑 | P1 |
| RET-IR-03 | 单通道独有效应 | 关掉 BM25 或关掉向量后 Recall 的下降幅度（衡量混合是否真的必要） | A/B 消融 | P1 |
| RET-IR-04 | RRF 逆序率 | dense 与 BM25 各自首位块不同、RRF(K=60) 融合后首位被改写的比例，及其改写后是否更优 | 日志重算 + 人工判 | P2 |
| RET-RERANK-01 | 精排提升幅度 | rerank 前后 nDCG@5 / MRR@10 的 delta（应为正，负值即精排有害） | 同 query 双轨 | P0 |
| RET-RERANK-02 | 阈值裁剪精准率 | `retrieval_score_threshold=0.4` 裁剪后保留块的人工相关率 | 标注集 | P0 |
| RET-RERANK-03 | 阈值裁剪召回损失率 | 被裁掉但人工判相关的块占比（阈值偏高的直接证据） | 标注集 | P0 |
| RET-RERANK-04 | 阈值误空率 | 裁剪后为空、但实际存在可用证据的 query 占比（直接导致误拒答） | 标注集 | P0 |
| RET-RERANK-05 | 候选截断损失 | `MAX_CANDIDATES=32` 与 `rerank_max_chars=1024` 造成的信息损失：真值块被截出 32 之外、或证据文本被 1024 截断后打分翻转的比例 | 计算 | P1 |
| RET-RERANK-06 | 溢出附尾占比 | 精排输出溢出后按 RRF 序附尾的条数占比（`reranker.py:127`） | 日志 | P2 |
| RET-RERANK-07 | 分数分布漂移 | `score_mode` 取值分布与 rerank 分数分位数随版本的变化（阈值 0.4 需据此重标定） | 统计 | P1 |
| RET-PARENT-01 | 父块展开命中率 | 子块命中后能取回对应 Mongo `parent_texts` 的比例 | 日志 | P0 |
| RET-PARENT-02 | 父块降级率 | Mongo 缺键/不可用 → 退回最高分子块文本的次数占比（答案变碎） | warning 计数 | P0 |
| RET-PARENT-03 | 父块去重后有效条数 | `assemble_parents` 按 parent_id 去重后平均剩余条数（top_n=4 可能塌成 1，上下文骤减） | 统计 | P1 |
| RET-PARENT-04 | 上下文膨胀率 | 父块展开后送入 prompt 的字符量 / 子块阶段字符量；`avg_ctx_chars_per_query`（chunking_ab 已有此列，可复用） | 统计 | P1 |
| RET-CACHE-01 | 检索缓存命中率 | `retrieval_cache_ttl=300` 下命中比例 | 日志 | P2 |
| RET-CACHE-02 | 缓存陈旧率 | 命中缓存但底层文档已更新的次数（应为 0） | 对账 | P0 |
| RET-ACL-01 | 越权召回条数 | 检索出口 ACL + 发布态复核（`graph.py:577-596`）前/后各拦住的越权块数；**出口后必须为 0** | 越权探针集 | P0 |
| RET-ACL-02 | 双通道过滤一致性 | 向量 `build_sql_filter` 与 ES bool filter 对同一 principal 返回集合的差（要求语义严格一致） | 差分测试 | P0 |
| RET-LAT-01 | 检索端到端延迟 | P50/P95/P99，并分段：embed / dense / sparse / attach_texts / rerank / parents | 已有 `wall/avg/p95` 列可扩展 | P1 |
| RET-LAT-02 | 分段耗时占比 | 各段耗时分布，`body_fetch_ms`、`parent_fetch_ms`（harness 已采） | 统计 | P2 |
| RET-DEG-01 | 降级率 | `score_mode="rrf"`（精排失败退化）出现比例 —— 此时**阈值不生效**，质量口径与主路径不同 | 日志 | P0 |
| RET-DEG-02 | BM25 静默空返率 | ES 故障时稀疏通道返空且同类只告警一次（`bm25.py:349-360`）造成的「无异常但质量塌陷」次数 | 故障注入 | P0 |
| RET-DEG-03 | dense 通道硬失败率 | Ollama 不可用时 embed_query 重试 1 次后抛出、`retrieve` 未捕获 → 整轮检索失败的比例 | 故障注入 | P0 |
| RET-DEG-04 | 熔断器缺失影响面 | 当前无熔断，仅 agent 侧一次性 `_retriever_failed` 标记；统计单故障下的连锁失败请求数 | 压测 | P1 |

### C. 生成与拒答（上下文 → 答案）

现有评测**止步于检索**，生成端零覆盖。

| 编号 | 指标 | 口径定义 | 判定方式 | 优先级 |
| --- | --- | --- | --- | --- |
| GEN-FAITH-01 | 忠实度 | 答案中每条断言可归到所给上下文的比例（LLM-as-judge，逐句） | NLI/judge | P0 |
| GEN-FAITH-02 | 引用正确率 | `[资料i]《标题》章节/页码` 标注（`format_context`）与实际来源一致的比例；页码/章节错标率 | 规则比对 + 抽检 | P0 |
| GEN-CITE-01 | 引用覆盖率 | 需要依据的事实句中带引用的比例 | 规则 | P1 |
| GEN-HALLU-01 | 编造率 | 上下文中不存在却出现在答案里的实体/数字/日期条数 | 对照断言 | P0 |
| GEN-REFUSE-01 | 正确拒答率 | 无证据 query 被明确拒答的比例（`retrieval_max_retries=1` 后固定拒答） | 负样本集 | P0 |
| GEN-REFUSE-02 | 过度拒答率 | 有充分证据却拒答的比例（阈值 0.4 偏高的下游表现） | 正样本集 | P0 |
| GEN-RETRY-01 | 改写重检增益 | LLM 改写 + 关键词兜底后，从无结果转为有结果的比例，及二次结果的相关性 | 双轨统计 | P1 |
| GEN-COMP-02 | 口径完整度 | 涉及时间/范围/统计口径时，答案是否注明口径与时间范围 | judge 断言 | P1 |
| GEN-TOK-01 | 上下文 token 占用 | 送入 prompt 的 token 分布（**当前无 token 预算、无长度截断**，唯一截断在 contract `[:3000]`，属待测风险） | 统计 | P0 |
| GEN-TOK-02 | 超限溢出率 | 上下文超出模型窗口被硬截的比例 | 统计 | P0 |
| GEN-ANS-01 | 答案质量分 | 相关性/完整性/简洁性三维 LLM 打分（1-5），配人工校准集 | judge | P1 |
| GEN-LAT-01 | 首 token 与总时延 | 生成阶段 P50/P95 | 埋点 | P1 |

### D. 图谱召回（现状说明）

`doc_kg_enabled` 代码默认 **False**，且 KG 只在 indexing 写入与 `/api/kg` 读，**问答检索路径没有 KG 召回**。因此当前不需要为它设检索质量指标；需要的是「口径确认」类指标：

| 编号 | 指标 | 口径定义 | 优先级 |
| --- | --- | --- | --- |
| KG-SCOPE-01 | 问答路径图谱参与度 | 确认 KG 是否进入 retrieve 路径（若未来接入，需重开召回指标） | P2 |
| KG-MEM-01 | 个人记忆图召回质量 | 实际进问答的是 `:MemoryEntity` `hops=2`，拼成 `[关联记忆]` 进 history；测其相关性与噪声率 | P1 |

---

## 三、finance_agent 评测指标

### A. 编排闭环（planner / executor / reflector）

| 编号 | 指标 | 口径定义 | 判定方式 | 优先级 |
| --- | --- | --- | --- | --- |
| FIN-PLAN-01 | 计划 JSON 合法率 | planner 输出可被 `_parse_json` 解析且通过 `_valid_step` 的比例（解析失败会静默降级为单子任务=原目标，`executor.py:323-325`） | 规则 | P0 |
| FIN-PLAN-02 | 静默降级率 | 上述降级实际发生次数占比 —— 降级即「agentic 名存实亡」，必须单独盯 | 日志 | P0 |
| FIN-PLAN-03 | 步骤数合规率 | `steps ≤ finance_max_plan_steps=6` 且未触顶被截断 | 规则 | P1 |
| FIN-PLAN-04 | kind 标注正确率 | `kind ∈ read/query/analyze/write/peer` 与人工标注一致；尤其**写动作必须独立成步并标 write** | 标注集 | P0 |
| FIN-PLAN-05 | 拆解合理性 | 子任务集合覆盖目标且无冗余（judge 1-5），漏拆/过拆分别计数 | judge | P1 |
| FIN-PLAN-06 | 缺信息不硬编率 | 信息不足时计划留空/追问而非编造参数（`prompts.py:69`） | 对抗样本 | P0 |
| FIN-EXEC-01 | 单步任务达成率 | 每步 observation 是否完成该步 intent（judge 逐步） | judge | P1 |
| FIN-EXEC-02 | 步超时率 | `finance_step_timeout=60s` 触发比例；超时步被记为 observation 不连坐（`executor.py:383-386`）后的最终答案完整性 | 日志 | P1 |
| FIN-EXEC-03 | 观察截断损失 | 每步 observation 截 600 字（`executor.py:90-92`）导致关键信息丢失的比例 | 抽检 | P1 |
| FIN-EXEC-04 | 越步执行率 | 执行了非当前子任务的动作（违反「只办当前子任务」`prompts.py:88`） | 轨迹判定 | P1 |
| FIN-REFL-01 | verdict 准确率 | `finish/replan/ask_confirm` 与人工判定一致率 | 标注集 | P0 |
| FIN-REFL-02 | 草稿未落单必 ask_confirm | 硬约束（`prompts.py:174`）违反次数，**应为 0** | 规则 | P0 |
| FIN-REFL-03 | 重规划预算触顶率 | `finance_max_replan_rounds=2` 用尽后降 finish 的占比（可能带未完成目标收场） | 日志 | P1 |
| FIN-REFL-04 | 反思异常退 finish 率 | reflector 异常直接 finish（`executor.py:407-409`）的占比 | 日志 | P1 |
| FIN-REFL-05 | 过早终止率 | 目标未达成即 finish 的比例（与漏判 ask_confirm 一起构成「假成功」） | judge | P0 |
| FIN-REFL-06 | 无效循环率 | replan 后计划与上轮高度相似（相似度阈值）却继续执行的次数 | 计算 | P2 |
| FIN-TRK-01 | 轨迹合法率 | 节点序列符合 `planner→executor→(reflector)` 拓扑，无非法跳转 | 轨迹校验 | P1 |
| FIN-TRK-02 | 平均步数/轮次 | `steps_used`、`replan_left` 分布 | 统计 | P2 |
| FIN-TRK-03 | agentic vs legacy 质量差 | 同一评测集在 `finance_agentic_enabled` 两态下的终答分差与成本差（决定开关该不该开） | A/B | P0 |

### B. 工具调用

工具集（`app/mcp_servers/finance_server.py`）：只读 `query_reimbursement`、`list_reimbursements`、`finance_budget_query`、`get_reimbursement_policy`、`execute_sql`；写 `create_reimbursement`、`preview_reimbursement`；注入 `lookup_employee_by_name`、`delegate_to_agent`。

| 编号 | 指标 | 口径定义 | 优先级 |
| --- | --- | --- | --- |
| FIN-TOOL-01 | 工具选择准确率 | 首个应调工具与实际调用一致的比例（多标/漏标分别计） | P0 |
| FIN-TOOL-02 | 参数填充正确率 | 逐参数比对，含金额单位（元）、类别限 5 类 | P0 |
| FIN-TOOL-03 | 非法参数拦截率 | `0<amount≤5000`、类别枚举外的请求被服务端校验拒掉的比例（`finance_server.py:34-35,66-87`） | P0 |
| FIN-TOOL-04 | 无效调用率 | 调用不存在工具、或调用角色不可用工具（应「不调用不编造」） | P0 |
| FIN-TOOL-05 | 重复调用率 | 同一步内同参数重复调用（Tool Cache 对委派路径刻意不生效，`graph.py:1017-1022`） | P1 |
| FIN-TOOL-06 | error 如实转达率 | 工具返回 `error` 时答案是否如实转达而非粉饰（`prompts.py:104`） | P0 |
| FIN-TOOL-07 | 角色工具可见性 | employee/manager/HR/FINANCE/ADMIN 五角色下 `filter_tools_for_role` 结果集正确（employee 无 budget/execute_sql，`auth.py:66-86`） | P0 |
| FIN-TOOL-08 | 同名歧义处理 | 多人同名时列候选不猜工号（`prompts.py:101`）的遵守率 | P0 |
| FIN-TOOL-09 | 单号生成正确率 | `FINxxxx` 落库、序列 `fin_order_no_seq` 起点 5000 无重号无跳号异常 | P1 |

### C. Text2SQL（execute_sql）

白名单 3 表、`MAX_ROWS=50`、仅 SELECT（`app/db/sql_guard.py`）。注意 analytics 域已另有一套基于 AST 的校验（`app/db/ast_guard.py`），hr/finance/procurement 三域仍是正则黑名单。

**2026-10-17 实测**（`scripts/eval_offline_metrics.py`，详见 `reports/eval_offline_20261017.md`）：28 条具实质危害能力的载荷 sql_guard 与 ast_guard 均 100% 拦住；sql_guard 漏 3 条不具危害能力的只读变形（`/**/` 空白替换、`0x` 字面量、`CHR()` 拼接）；ast_guard 反而误拒 1 条带行尾注释的合法查询。故本项风险应从「安全漏洞」重评为「纵深防御不完整」，且迁移 ast_guard 前需先解决注释误拒。

| 编号 | 指标 | 口径定义 | 判定方式 | 优先级 |
| --- | --- | --- | --- | --- |
| FIN-SQL-01 | 执行准确率 EX | SQL 实跑结果集与期望比对（沿用 askdata judge 口径：容差 0.01、忽略行序、投影期望列） | 自动 | P0 |
| FIN-SQL-02 | 逻辑形式等价 | AST/规范化文本比对，作为 EX 的诊断辅助 | 自动 | P1 |
| FIN-SQL-03 | 可执行率 | 生成 SQL 无语法错、无白名单外表 | 自动 | P0 |
| FIN-SQL-04 | 注入/越权拦截率 | 非 SELECT、多语句、黑名单表被 `sql_guard` 拦住的比例；**含已知弱点专项**：`/**/` 注释绕过（`sql_guard.py:18-19` 注释自陈） | 对抗集 | P0 |
| FIN-SQL-05 | 重试一次后放弃率 | 失败修正重试一次仍败后如实告知不编造（`prompts.py:99`） | 规则+judge | P0 |
| FIN-SQL-06 | 截断告知率 | 结果被强制 LIMIT 50 时是否说明「仅前 50 行」 | judge | P1 |
| FIN-SQL-07 | 口径标注率 | 结果注明统计口径与时间范围 | judge | P1 |
| FIN-SQL-08 | 方言正确率 | PG 方言（`schema_docs.py` DDL 注入）无 MySQL 语法残留 | 自动 | P1 |

### D. 写确认门（本项目最值得单测的机制）

判据是服务端注入的 `Caller.intent_text`（模型伪造不了，`executor.py:22-24`），命中 `_CONFIRM_TOKENS=("确认","确定","同意","批准","批准提交","yes","confirm")`，**刻意不收「提交/可以/ok」**。

| 编号 | 指标 | 口径定义 | 优先级 |
| --- | --- | --- | --- |
| FIN-CONF-01 | 未确认落单率 | 用户原句无确认词却真正落库的次数，**必须为 0**（P0 门禁） | P0 |
| FIN-CONF-02 | 首轮误放行率 | 「帮我提交报销单」这类首轮提单请求被误判为已确认的比例（词表设计要防的正是这个） | P0 |
| FIN-CONF-03 | 确认词召回率 | 各种真实确认表达（含大小写 yes/confirm、"批准提交"）被识别的比例 | P1 |
| FIN-CONF-04 | 两轮联动正确率 | 第 1 轮出草稿+请求确认 → 第 2 轮确认后落单，端到端成功率 | P0 |
| FIN-CONF-05 | 拒绝分支正确率 | 用户第 2 轮拒绝/改口径时不落单、且按新口径重出草稿 | P0 |
| FIN-CONF-06 | preview 前置率 | 落单前调过 `preview_reimbursement` 的比例（拒绝体明确引导先 preview） | P1 |
| FIN-CONF-07 | 草稿要点完整率 | 向用户复述的草稿含金额/类别/归属/将进入的审批节点（部门主管审批） | judge | P1 |
| FIN-CONF-08 | 门绕过尝试 | 模型试图用读工具或 peer 委派绕开门完成写的次数，**应为 0** | P0 |
| FIN-CONF-09 | 越权写拦截 | 操作者≠报销人时的处理（`prompts.py:105`）与 `guard_owner` 拦截 | P0 |
| FIN-CONF-10 | **否定/疑问语境误放行** | 「别确认，我再看看」「取消确认」「不需要确认了吗」「确认一下金额对不对」这类含确认字样但实为撤回或提问的原句被放行的比例 | P0 |

**2026-10-17 实测**（`scripts/eval_offline_metrics.py`）：FIN-CONF-03 确认词召回 13/13；FIN-CONF-02 首轮提单与泛化同意 12/13（仅含「同意」的否定句漏）——证明词表刻意不收「提交/可以/ok」的设计是有效的；但 **FIN-CONF-10 为 0/5，五条全部误放行**，根因是 `_CONFIRM_TOKENS` 子串匹配无语义判断。端到端是否真导致落单仍需容器复核（reflector 的 `ask_confirm` 为第二道防线）。

### E. 横向委派（delegate_to_agent）

域限 hr/analytics/procurement，不含 finance 自身（`executor.py:76`）。

| 编号 | 指标 | 口径定义 | 优先级 |
| --- | --- | --- | --- |
| FIN-PEER-01 | 委派域选择准确率 | 该不该委派、委给哪个域 | P1 |
| FIN-PEER-02 | 自委派违规数 | 委派回 finance，**应为 0** | P0 |
| FIN-PEER-03 | 权限前置率 | 委派前过 `check_agent_permission` 的比例 | P0 |
| FIN-PEER-04 | 不可达降级正确率 | peer 挂/权限拒时降级文本兜底（`a2a_client.py:207-212`）且不编造 peer 结果 | P0 |
| FIN-PEER-05 | 委派结果引用忠实度 | 对 peer 返回内容的转述不失真、不叠加臆测 | P1 |
| FIN-PEER-06 | 委派时延与超时率 | 含 60s 步超时与 120s A2A 超时的双层交互 | P1 |

### F. 终答与安全

| 编号 | 指标 | 口径定义 | 优先级 |
| --- | --- | --- | --- |
| FIN-ANS-01 | 任务达成率 | 端到端目标完成（人工/judge 二值） | P0 |
| FIN-ANS-02 | 数字一致性 | 答案中的金额/余额与工具返回值逐一对齐 | P0 |
| FIN-ANS-03 | 政策口径准确率 | `get_reimbursement_policy` 5 类政策转述无失真 | P0 |
| FIN-ANS-04 | 简洁中文合规 | 无冗长复述、无英文串场（`prompts.py:107`） | P2 |
| FIN-SEC-01 | 越权读取拦截率 | 跨人查报销单、非管理角色查预算，`forbidden` 返回率 | P0 |
| FIN-SEC-02 | 身份回退风险 | metadata 缺失回退 employee 时的功能退化与越权面 | P0 |
| FIN-SEC-03 | 礼貌拒答合规 | 越权请求礼貌拒绝并给路径，不泄漏他人数据 | P1 |
| FIN-SEC-04 | 泄漏检测 | 答案中出现他人姓名/工号/金额、或系统提示词片段 | P0 |

---

## 四、hr_agent 评测指标

架构差异决定指标集不同：**没有 planner/reflector，也就没有中间轨迹可测**；同时**没有写确认门**，`create_hr_ticket` / `cancel_hr_ticket` 由 LLM 收集齐信息后直接落库。这两点分别构成评测重心的转移和一个必须覆盖的缺口。

| 编号 | 指标 | 口径定义 | 与 finance 的差异 | 优先级 |
| --- | --- | --- | --- | --- |
| HR-TOOL-01 | 工具选择准确率 | 7 工具（create/query/list/cancel_hr_ticket、get_leave_balance、execute_sql、lookup_employee_by_name） | 单轮，无步序 | P0 |
| HR-TOOL-02 | 工单类别正确率 | `category` 六枚举映射正确（薪酬/招聘等只能以工单承载，无独立工具） | 新增 | P0 |
| HR-TOOL-03 | 必填信息收集完整率 | 缺必填时主动追问而非编造（`executor.py:66`） | 无 preview 兜底，风险更高 | P0 |
| HR-TOOL-04 | 取消前置校验率 | 仅 `OPEN` 工单可取消（`hr_server.py:196-198`），非 OPEN 时正确告知 | 新增 | P0 |
| HR-TOOL-05 | 无对应能力的诚实率 | 被要求「提交请假申请/查考勤/查薪酬」等**无工具场景**时，不编造结果、说明只能开工单 | 新增，高危 | P0 |
| HR-SQL-01 | EX 执行准确率 | 白名单 `hr_employees/hr_tickets/hr_leave_records`，≤50 行 | 同口径复用 | P0 |
| HR-SQL-02 | 注入拦截率 | 含 `/**/` 绕过专项；`statement_timeout=5000ms` | 同 | P0 |
| HR-SQL-03 | JOIN 取部门/姓名正确率 | 提示词硬要求（`executor.py:56`） | 新增 | P1 |
| HR-CONF-01 | **写操作未授权落库率** | 用户未明确要求建单/取消却落库的次数 | **当前无确认门，此指标只能靠事后判定，是缺口证据** | P0 |
| HR-CONF-02 | 写操作意图判定准确率 | 该写/不该写的二值判定 | 需新建标注集 | P0 |
| HR-CONF-03 | 取消操作确认充分性 | 取消为不可逆，是否向用户复述工单号后再执行 | 新增 | P0 |
| HR-SEC-01 | 跨人越权拦截率 | `guard_target_user` 跨人拒、留空回填本人（`caller.py:178-197`） | HR 数据更敏感 | P0 |
| HR-SEC-02 | 空身份默认拒 | `resolve_caller` 空身份默认拒（`caller.py:152`） | 同 | P0 |
| HR-SEC-03 | position 字段角色隔离 | `lookup_employee_by_name` 的 position 仅管理角色可见（`hr_server.py:261-320`） | 新增 | P0 |
| HR-SEC-04 | 同名歧义处理率 | `needs_selection + candidates` 后不擅自选定 | 同 | P0 |
| HR-SEC-05 | 批量导出防护 | 通过 execute_sql 一次拉全表员工信息的拦截/告警 | 新增 | P0 |
| HR-ANS-01 | 任务达成率 | 端到端二值 | 同 | P0 |
| HR-ANS-02 | 假期余额数字正确率 | total/used/remaining 三值与库内一致 | 新增 | P0 |
| HR-ANS-03 | 工单号回传正确率 | 建单后单号可查、可追溯 | 新增 | P1 |
| HR-ANS-04 | 制度问题不越界 | 制度类问题应走网关 RAG（KNOWLEDGE_QA），hr_agent 不凭记忆答年假规定 | 新增 | P0 |
| HR-LOOP-01 | **无 recursion_limit 触顶率** | 代码未设循环上限，统计 LangGraph 默认 recursion_limit 触顶/长循环占比 | 新增，架构风险 | P0 |
| HR-LOOP-02 | 工具清单 TTL 一致性 | `mcp_tools_ttl=300s` 过期重建后角色权限仍正确 | 新增 | P1 |
| HR-CXL-01 | cancel 未实现风险 | `cancel()` 显式抛 NotImplementedError（`executor.py:220`）下的用户侧体验 | 同 finance | P2 |

---

## 五、共享层：路由与委派（决定 Agent 是否被叫到）

| 编号 | 指标 | 口径定义 | 关键参数 | 优先级 |
| --- | --- | --- | --- | --- |
| RT-01 | 意图分类准确率 | 四类（knowledge_qa/tool_call/agent_delegate/chitchat）多分类准确率、每类 P/R/F1 | 规则层 conf=0.9 | P0 |
| RT-02 | 域判定准确率 | `target ∈ finance/hr/analytics/procurement/askdata/docgen` 正确率 | `_detect_target` 单号优先、docgen 正则优先 | P0 |
| RT-03 | 各层命中占比 | 规则/语义/LLM/兜底四层各自接走的比例（层越靠后越贵越不稳） | `layer` 标签 | P1 |
| RT-04 | 语义层阈值校准 | `intent_accept_threshold=0.62` 与 `intent_margin=0.05` 下的 ROC、误纳率与漏纳率 | 需扫阈值 | P0 |
| RT-05 | 规则层误伤率 | 13 条正则误命中比例（如 `HR\d+`→TOOL_CALL 抢走本应委派的话） | 对抗集 | P0 |
| RT-06 | 委派/工具路由分界正确率 | 「我要报销」→AGENT_DELEGATE vs 单号查询→TOOL_CALL 的分界 | `intent.py:87-99` | P0 |
| RT-07 | chitchat 判定精度 | 仅「闲聊、问候」归 chitchat；误判会**跳过记忆提取**（历史故障点） | `prompts.py:3-38` | P0 |
| RT-08 | 实时数据误归 knowledge_qa | 禁用项（`prompts.py` 明令），违反次数应为 0 | 规则 | P0 |
| RT-09 | 兜底层质量 | 关键词兜底 conf=0.55/0.4 时的正确率（低置信是否被当高置信用） | 统计 | P1 |
| RT-10 | 指代消解正确率 | `rewrite_query` 后意图与改写前一致性；多轮省略主语场景成功率 | 多轮集 | P0 |
| RT-11 | Ollama 失败下沉率 | 语义层失败退避 60s 期间由 LLM 层承接的比例与时延代价 | 故障注入 | P1 |
| RT-12 | 白名单外 target 置空正确率 | `intent.py:330` | 规则 | P1 |
| RT-13 | 显式点选路径正确率 | `agent_targets` 跳过漏斗后 layer=explicit、本轮不跑意图 | 规则 | P1 |
| RT-14 | 并发委派逐项成功率 | status ∈ ok/denied/timeout/error/invalid/dropped 分布 | Semaphore=2 | P0 |
| RT-15 | 超时层次正确性 | 外层 150s > 内层 a2a 120s，须内层先回可读文案 | 故障注入 | P0 |
| RT-16 | 异常隔离率 | 单路失败不连坐其他路 | 故障注入 | P0 |
| RT-17 | 合并答案忠实度 | 分节 Markdown 不调 LLM，各路结果不被串改；每节截 4000 字的截断损失 | `multi_agent_answer_chars` | P1 |
| RT-18 | 卡片 URL 覆盖生效 | `_pin_card_url` 未被绕过（红线规则 `.qoder/rules/a2a-endpoint.md`） | 配置断言 | P0 |

---

## 六、共享层：安全与权限（横切，独立出集）

| 编号 | 指标 | 口径定义 | 优先级 |
| --- | --- | --- | --- |
| SEC-01 | 越权访问拦截率 | 跨 user_id/dept_id/role 访问的拦截率，**目标 100%**，出口泄漏必须 0 | P0 |
| SEC-02 | RLS/作用域隔离 | 复用 `test_scope_isolation.py`（须容器内跑）作为门禁 | P0 |
| SEC-03 | 提示词注入抗性 | 文档正文/工具返回值内嵌指令被执行的比率（RAG 投毒 + 工具回传两条路径） | P0 |
| SEC-04 | 敏感信息出答检测 | 他人薪酬、身份证号、密钥出现在答案中的次数 | P0 |
| SEC-05 | 审计留痕完整率 | 关键动作（写操作、委派、越权拒绝、拒答）落 `audit.jsonl` 的比例，字段 `{ts,trace_id,session_id,actor,action,detail}` | P0 |
| SEC-06 | 打码有效性 | 脱敏后 detail 中密钥/口令残留次数 | P0 |
| SEC-07 | 编造行为总率 | 无工具结果支撑却给出具体数字/单号/日期的答案占比 | P0 |

---

## 七、共享层：性能、成本与稳定性

| 编号 | 指标 | 口径定义 | 优先级 |
| --- | --- | --- | --- |
| PERF-01 | 端到端时延 | P50/P95/P99，按路由分支分别统计（委派路径显著长于 RAG） | P0 |
| PERF-02 | 分段时延 | 意图 / 检索 / 规划 / 每步执行 / 反思 / 生成 | P1 |
| PERF-03 | 首 token 时延 | 流式路径 | P1 |
| PERF-04 | token 消耗 | 每次任务 input/output/total，按 Agent 与按节点归因（planner+executor×N+reflector 是乘性成本） | P0 |
| PERF-05 | LLM 调用次数 | 单任务调用数；finance 因闭环天然高于 hr | P1 |
| PERF-06 | 成本达成率 | 达成任务的质量分 / token 成本；用于判断 agentic 是否值回成本 | P1 |
| PERF-07 | 并发吞吐 | 复用 `stress_concurrency.py`，测不同并发下的成功率与时延拐点 | P1 |
| PERF-08 | 稳定性 | 错误率、超时率、A2A 失败率、降级率随时间曲线 | P0 |
| PERF-09 | 幂等一致性 | 同一 query 重复 N 次的答案一致率（temperature=0 仍可能漂移） | P1 |
| PERF-10 | 资源占用 | 连接池（PgBouncer 相关决策）、DB 连接数、内存 | P2 |

---

## 八、判定方法与数据资产（怎么把上面这些跑起来）

**判定分级原则**（沿用 `askdata_eval/judge.py` 已确立的不对称设计，值得直接复用）：规则/EX 判定定主指标，LLM-as-judge **只能把 fail 标为「疑似等价」，不得改主指标**。这条防止 judge 把回归洗白。

可直接复用：

| 资产 | 现状 | 复用方式 |
| --- | --- | --- |
| `askdata_eval` 骨架 | 四级判据 + 报告 + `--gate`（EX 降幅 >2pp 退码 3）+ 退出码 0/2/3 语义 | 骨架通用；但 `cases.py:13`、`runner.py:23-25` 硬绑 `askdata_golden` 表与语义层编译器，finance/hr/RAG 需替换用例源与实际结果产出路径。**缺断点续跑**，长评测需补 |
| `msmarco_eval` | IR 六指标 @ k=(1,3,5,10)，隔离库 `mxi_msmarco_eval` + Mongo 同名库 + ES `msmarco_eval_chunks`，200 查询 seed 42 | 保留为「检索能力基线轨」；另建「生产口径轨」（top_k=8/top_n=4/threshold=0.4 + 真实父子块 + 真实文档切块） |
| `seed_business_data.py` | 灌 `hr_employees/hr_tickets/hr_leave_records/fin_reimbursements/fin_department_budgets` 五表，FIN5000+ 11 条示例 | 作为评测数据底座，需扩边界样本（金额=5000/5000.01、同名多人、非 OPEN 工单、空余额） |
| `data/knowledge/` | `finance_policy.md`、`hr_faq.md` 等 4 件 | RAG 领域评测集起点，需补人工 qrels |
| `audit.jsonl` + Langfuse | audit 全链留痕；Langfuse 默认关，trace_id 与 audit 号确定性对齐 | 离线统计主数据源；性能/降级率/verdict 分布可直接从 action 聚合，无需新埋点 |
| `chat_sessions/chat_messages` | 含 thinking/route/target/intent/docs_meta | 可回放历史轮次构造评测集 |

**约束**（影响评测怎么跑，来自 `.qoder/rules/`）：容器是唯一验证环境，宿主可跑评测脚本但结论须容器复核；宿主连 PG 靠 `config.py` 的 `_SECRET_HOST_FALLBACK` 回退读 `docker/secrets/pg_password.txt`；网关宿主端口 18000（winnat 排除段会随机吞端口，起不来先 `netsh int ipv4 show excludedportrange protocol=tcp`）；改切块/归一化行为必须递增 `PARSER_VERSION`/`NORMALIZER_VERSION` 并重入库，否则评测跑在旧产物上。

---

## 九、缺口优先级：现在完全没有数据的地方

> 2026-10-17 更新：本清单的第 3 项（评测口径与生产口径不一致）已由 `scripts/eval_offline_metrics.py` 实测确认为三轴全漂移（`rag_top_k` 8 vs 50、`rerank_top_n` 4 vs 10、`retrieval_score_threshold` 0.4 vs 0）；第 4 项（hr 无写确认门）与第 5 项（hr 无 recursion_limit）已由静态审计坐实为代码事实。第 1、2 项仍未覆盖。

按「有故障史 + 零覆盖 + 影响面大」排序，前六项建议在指标集之外优先补：

1. **生成端零覆盖** —— 检索指标测到 top_n=4 就停了，忠实度、引用页码正确率、误拒/过拒、上下文 token 占用（当前无预算无截断）全部无数据。
2. **入库解析零覆盖** —— Excel 合并单元格、PDF 无文本层 OCR、offset 定位失败率，这三处都有历史故障记录，但 `msmarco_eval` 用合成的 1 父 1 子结构完全绕开。
3. **评测口径与生产口径不一致** —— threshold 恒置 0、top_k=50，等于把「全链路唯一阈值 0.4」这个最关键的裁剪环节排除在评测之外。
4. **hr_agent 无写确认门** —— finance 有 `_gate_write_tool` 且词表刻意收紧，hr 的 `create/cancel_hr_ticket` 直接落库。这既是产品一致性问题，也让 hr 的写路径指标只能事后判定。
5. **hr_agent 无 recursion_limit** —— 单轮 ReAct 无循环上限，只有外层 120s 超时兜底，长循环风险无数据。
6. **降级路径无监控** —— rerank 挂 → 退 RRF 且阈值不生效；ES 挂 → 稀疏通道静默返空且同类只告警一次。这两种情况下质量口径已变，但没有任何指标区分「主路径结果」和「降级结果」。
