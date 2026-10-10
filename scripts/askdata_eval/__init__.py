"""智能问数离线评测: 自建 golden set 的执行准确率(EX) + 两级 LLM judge。

跑法::

    uv run python -m scripts.askdata_eval                     # DSL 编译回归(不碰 LLM, 最快)
    uv run python -m scripts.askdata_eval --mode agent         # 端到端(含槽位抽取与 DSL 生成)
    uv run python -m scripts.askdata_eval --limit 50 --tag 环比

两种模式的分工(不是"一个更高级的版本"):
- ``dsl``: 输入是 golden 里存好的 DSL, 只跑"校验 -> 编译 -> 执行 -> 比结果集"。
  它没有 LLM, 所以**每次改编译器/语义层都能确定性重跑**, 是回归门禁该用的模式。
- ``agent``: 输入是自然语言问法, 走完整状态机(槽位抽取 + 链接 + DSL 生成 + 编译 + 取数)。
  它测的是"人话到数字"的整链路准确率, 但每跑一次都要花 LLM, 且同输入可能不同输出 ——
  所以分数只用于趋势判断, 不适合当硬性门禁。

判据口径: 先比列(名字+顺序), 再交 LLM 比 dataframe(EX 只比行集), 两级任一不过即算错。
LLM/Ollama 不可用时自动降级成"只跑 EX"并在报告首行标注 —— 缺判据要显式说, 不能默默出分。
"""
