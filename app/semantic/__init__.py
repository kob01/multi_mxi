"""语义层(指标中台): 智能问数的单一口径事实源。

按"是否需要数据库"切成两半, 这条界线是硬的:

- **纯函数半区**(可被 AskData_Agent 进程 import, 该容器按层 0 红线不持 DB 凭据):
  :mod:`app.semantic.dsl`(DSL 形状)、:mod:`app.semantic.timesem`(时间语义解析)、
  :mod:`app.semantic.validate`(三级校验, catalog 由调用方注入)、
  :mod:`app.semantic.seed`(内置口径集)。
- **DB 半区**(只允许 askdata MCP server 调用): :mod:`app.semantic.registry`
  (读 sem_* 三表)、:mod:`app.semantic.link`(三路召回)、
  :mod:`app.semantic.compiler`(DSL -> SQL)。

于是"模型不许写 SQL"这件事不是叮嘱出来的: agent 手里根本没有能连库的代码路径,
它能做的只有"交一份 DSL 上去, 由服务端决定怎么算"。
"""
