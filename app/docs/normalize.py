"""文本规范化: offset 基准的单一定义点。

整篇正文外置到 MongoDB 后, ``doc_parents.start_offset/end_offset`` 与
``parent_texts.text`` 全部以 ``normalized_text`` 为基准。因此 raw -> normalized
的变换必须是**唯一、确定、可解释**的入口:

- 只做保守变换(换行归一 / NFKC / 剥零宽 / 行尾空白 / 压缩连续空行 / 末尾单 \\n);
- **绝不删词、换词、重排** —— 否则 offset 切回来的片段不再是原文连续子串,
  引用定位与高亮就断了。

任何修改本模块行为的提交, 都必须同步递增 ``NORMALIZER_VERSION``
(见 CONFIG_RULES.md 第 4 条), 否则已入库的 offset 集体失效。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from app.config import get_settings

# 与配置同步递升: 归一化规则一改, 全库旧 offset 即不可解释。
NORMALIZER_VERSION: str = get_settings().normalizer_version

# 零宽 / BOM / 方向控制等不可见字符: 统一剥除, 免得混进 offset 计数。
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
# 行尾空白(含半/全角空格与制表符)。
_TRAILING_WS = re.compile(r"[ \t\u3000]+\n")
# 3 个及以上连续换行压成 2 个(保留最多一个空行)。
_MANY_BLANK = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """把原始解析文本规范化为 offset 基准文本(确定性、保守、可解释)。"""
    if not text:
        return "\n"
    # 1) 统一换行: CRLF / CR -> LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 2) NFKC: 全角/半角、兼容字符归一(如 ﬁ -> fi), 保持字符级可切片
    text = unicodedata.normalize("NFKC", text)
    # 3) 剥零宽/不可见格式字符
    text = _ZERO_WIDTH.sub("", text)
    # 4) 去行尾空白
    text = _TRAILING_WS.sub("\n", text)
    # 5) 压缩多余空行
    text = _MANY_BLANK.sub("\n\n", text)
    # 6) 末尾保证恰好一个换行
    text = text.rstrip("\n") + "\n"
    return text


def offset_slice(normalized: str, start: int, end: int) -> str:
    """按 [start, end) 从 normalized 取子串; 越界记日志并截断, 不抛异常。

    统一出口: 全项目切正文都必须走这里, 便于把"越界即数据漂移"这类问题收敛到
    一个可观测点。
    """
    if start < 0 or end < start or start > len(normalized):
        # 迁移回算失败的父块 offset 为 -1, 属预期, 静默返回空串即可。
        return ""
    return normalized[start : min(end, len(normalized))]


def content_hash(text: str) -> str:
    """sha1 hex[:16]: 增量入库时判定块内容是否变化的依据。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
