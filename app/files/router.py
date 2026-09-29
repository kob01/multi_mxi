"""生成物下载路由: GET /api/files/{token}/{file_name}(计划 E3)。

为什么不用 StaticFiles 直接挂 upload_dir: 那会把用户上传的知识库原件一并暴露且
绕过权限。这里只作用域 ``gen/<token>/`` 子树, 叠四层防护:
1. 令牌形状(32 位 hex, 不可猜测的能力令牌) —— 本期最小访问面, 身份绑定强校验列后续;
2. 文件名 basename + 安全字符集校验(复用 app.analytics.store.is_safe_name 的规则面);
3. ``resolve()`` 后断言仍在 gen/ 子树(挡符号链接与一切穿越写法);
4. 扩展名白名单 -> MIME 映射(``docgen.genstore.mime_of``): 未知后缀永远落不到
   ``application/octet-stream``, 因为白名单外的文件在 #2/#3 就已回 404。
   白名单不止 office 四类: 除 docx/xlsx/pptx/pdf 还有 md 与图片(png/jpg/jpeg)。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.docgen import genstore

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/{token}/{file_name}")
def download_generated(token: str, file_name: str) -> FileResponse:
    """按能力令牌下载一个生成物(Word/Excel/PPT/PDF/Markdown/图片)。"""
    path = genstore.build_path(token, file_name)
    if path is None:
        # 不区分"令牌不存在/文件不存在/形状非法": 免得被当成探测内网生成物清单的口子。
        raise HTTPException(status_code=404, detail="生成物不存在或已过期(保留期默认 24 小时)")
    ext = path.suffix.lower()
    return FileResponse(path, media_type=genstore.mime_of(ext), filename=path.name)
