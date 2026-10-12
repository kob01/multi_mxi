"""智能问数可观测面: OTel span 导出、Prometheus 指标、告警线。

三件事各自的位置要说清(否则容易变成"接了一堆东西但谁也看不懂哪个数从哪来"):

- **OTel**: 状态机每个节点的 span 与属性(置信度/召回分/route/重试数/tokens/semver)。
  开关 ``askdata_otel_enabled`` + ``otel_endpoint``; 没装 SDK 或没配地址时是**无操作**,
  与 Langfuse 那一路同一姿态(可观测不能成为业务链路的依赖)。
- **Prometheus**: ``GET /metrics`` 文本端点。指标源是**留痕表的聚合**而不是内存计数器 ——
  进程重启不丢历史、多副本聚合口径一致、且这些数与看板/REST 完全同源(不会出现"看板说
  澄清率 8%, 指标说 12%"这种两件事实)。
- **告警线**: 对同一份聚合判阈值, 既在 ``/metrics`` 里出 ``askdata_alert_firing`` 指标
  (Prometheus 端可以 alert on 它), 也在 ``/api/askdata/alerts`` 给人看当前是否越线。

与既有 Langfuse/audit.jsonl 的关系是**并存**而不是替换: trace 里看单次链路, 指标看趋势,
审计看合规; 三者按 trace_id 可以互相定位。
"""
