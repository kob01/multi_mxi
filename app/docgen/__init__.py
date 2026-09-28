"""文档生成子系统 (docgen): 结构化文字 + 图片 -> 可下载的 office/PDF/MD 文件。

单一交付路径(与 app/analytics 同构: 业务实现落本包, app/tools/docgen.py 做工具门面):
- ``docx_builder / xlsx_builder / pptx_builder / pdf_builder / md_builder``: 各格式的
  纯同步构建器, 入参是同一份 spec(title/sections/bullets/table/images); 图片只按解析层
  给来的**本地 PNG 路径**嵌入, 不在这里做网络/SSRF。
- ``images``: 图片解析层(async) —— 把 spec.images 里的本地文件或图片 URL 归一化成落到
  生成目录的 PNG(本地限定 report_dir/upload_dir, URL 过 url_guard 的 SSRF 护栏), 供上面
  的同步 builder 直接读。
- ``genstore``: 生成物落 upload_dir/gen/<token>/ + 能力令牌 + 到期清扫 + spec 解析;
  下载走 app/files 的 /api/files/{token}/{file}(不可猜测令牌, 只作用域 gen/ 子树)。

注: 原"网页创作工坊"(HTML 成品页 + Playwright 渲染沙箱 + 编辑器)已整体下线 —— docgen
的目标就是"文字+图片直接产出可下载的 docx/xlsx/pptx/pdf/md", 不再有网页这条岔路。
"""
