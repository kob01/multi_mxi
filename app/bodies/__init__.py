"""文档正文外置存储子系统 (MongoDB)。

整篇 raw/normalized/structure 与父块全文的唯一事实来源。与 ``app.rag`` 互不
import(避免环), 跨层编排只在 ``retriever.py`` 与 ``docs/service.py``。
"""
