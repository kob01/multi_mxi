"""生成物的落盘寻址、能力令牌与磁盘治理(Word/Excel/PPT/PDF/Markdown/图片 走这一套)。

与 ``app/analytics/store.py``(报告台账)刻意分开: office 生成物是"一次性交付物" ——
拿链接下载完就完, 不需要被"再找到", 因此不做台账、不做检索, 只靠不可猜测的能力令牌(uuid4 hex)
+ 保留期清扫治理生命周期。两套共存但互不混淆:
- 分析产物: ``report_dir`` 里的图表 SVG/PNG / 周报 MD / CSV, 落 report_artifacts 台账,
  走 /api/files/reports/{name} 回取("下次还能按人回查");
- 文件生成: ``upload_dir/gen/<token>/<file>``, `/api/files/{token}/{file}` 下载, 到期即删。

spec 解析也放这里: 各 builder 的入参都是"JSON 字符串", 解析失败要有统一、可回给
模型重试的错误文案(计划 E2: 入参结构化 spec, docstring 写清字段)。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from app.analytics.store import is_safe_name, stamp
from app.config import get_settings

logger = logging.getLogger(__name__)

# 能力令牌形状: 32 位 hex。下载路由据此硬校验, 保证 /api/files/reports/*(网页成品)
# 永远不会被本路由吞掉("reports" 不是 32 位 hex)。
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

# 允许生成的文件类型 -> MIME。白名单而不是通配: builder 只产出这些。
_EXT_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def new_token() -> str:
    """不可猜测的能力令牌: 知道 token 才能下载, 这是本期最小访问面(计划 E3)。"""
    return uuid.uuid4().hex


def _upload_dir() -> Path:
    """复用 docs.service 的上传根目录(同一卷、同一 mkdir 语义), 不另立配置。"""
    from app.docs.service import _upload_dir  # noqa: PLC2701 - 计划 E1 明确复用该实现

    return _upload_dir()


def gen_root() -> Path:
    """生成物根目录: upload_dir/gen/。"""
    path = _upload_dir() / "gen"
    path.mkdir(parents=True, exist_ok=True)
    return path


def gen_dir(token: str) -> Path:
    """某个令牌的专属目录; token 形状不合法直接抛 ValueError(防路径拼接注入)。"""
    if not TOKEN_RE.match(token or ""):
        raise ValueError("非法的生成令牌")
    path = gen_root() / token
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_file_name(ext: str, title: str = "") -> str:
    """生成物文件名: docgen-{yyyymmdd-hhmmss}-{slug}{ext}(与产物命名同一套安全规则)。"""
    suffix = (ext or "").lower()
    if suffix not in _EXT_MIME:
        raise ValueError(f"不支持的文件类型: {suffix or '(无扩展名)'}")
    return stamp("docgen", suffix, title)


def build_path(token: str, file_name: str) -> Path | None:
    """按令牌+文件名定位生成物; 任何形状不合法/越界/不存在都返回 None(下载路由转 404)。"""
    if not TOKEN_RE.match(token or "") or not is_safe_name(file_name or ""):
        return None
    root = gen_root().resolve()
    path = (root / token / file_name).resolve()
    # resolve 后断言仍在 gen/ 子树: 挡符号链接与一切穿越写法(计划 E3)。
    if root not in path.parents or not path.is_file():
        return None
    return path


def mime_of(ext: str) -> str:
    return _EXT_MIME.get((ext or "").lower(), "application/octet-stream")


def cleanup_expired() -> int:
    """删除超过保留期的生成目录; 返回删除个数。同步 IO, 调用方负责 to_thread。"""
    retention_hours = max(1, int(get_settings().docgen_retention_hours))
    deadline = time.time() - retention_hours * 3600
    removed = 0
    root = gen_root()
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < deadline:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError as exc:
            logger.warning("cleanup_expired skip %s: %s", entry.name, exc)
    return removed


def parse_spec(raw: Any) -> tuple[dict[str, Any] | None, str]:
    """解析工具入参的 spec(JSON 字符串或 dict); 失败返回统一错误文案。"""
    if raw is None or raw == "":
        return {}, ""
    if isinstance(raw, dict):
        return raw, ""
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "spec 不是合法 JSON 对象, 请按工具说明的结构重试"
    return (data if isinstance(data, dict) else None), (
        "" if isinstance(data, dict) else "spec 必须是 JSON 对象(如 {\"title\": ..., \"sections\": [...]})"
    )
