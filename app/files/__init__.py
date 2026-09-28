"""生成物下载包: 网关侧的 /api/files 路由(计划 E3)。

路由注册顺序敏感: assistant_router 里已有 ``/api/files/reports/{name}``(网页成品),
本包的 ``/{token}/{file_name}`` 必须注册在它**之后**; 且下载路由用 32 位 hex 令牌形状
硬校验, 即使顺序被调整, "reports" 也永远匹配不进令牌位(防吞路由的双保险)。
"""
