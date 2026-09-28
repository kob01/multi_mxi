"""进程内 web/docgen 工具离线冒烟: 不需要整栈运行, 不需要检索密钥。

跑法::

    uv run python -m scripts.smoke_tools          # 全量
    uv run python -m scripts.smoke_tools --keep   # 保留本次生成的产物文件

与计划的"测试与验收"清单对应关系:
- SSRF 拒绝面(验收第 4 项)全量离线可测: 内网字面名/私网 IP/云元数据/userinfo/非 http
  一律拒, 公网 IP 字面量(1.1.1.1/8.8.8.8)放行 —— 不依赖真实 DNS;
- 文件生成(验收第 5 项)测 builder 本体: 四类文件落盘 + 文件头魔数 + 重新可解析,
  以及令牌/路径穿越防护与保留期清扫;
- 联网检索(验收第 3 项)只验载荷形状与降级语义(ddgs 是否可达取决于当前网络);
- 路由回归(验收第 8 项)验证意图规则层: 新增 web/docgen 不抢 finance/hr/analytics 的既有判定。

ddgs 真实检索、mask_text 在真实回答上的表现, 由 scripts/test_tools_flow.py 走整栈验证。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

from app.assistant.intent import IntentRecognizer, IntentType
from app.docgen import genstore
from app.docgen.docx_builder import build_docx
from app.docgen.md_builder import build_md
from app.docgen.pdf_builder import build_pdf
from app.docgen.pptx_builder import build_pptx
from app.docgen.xlsx_builder import build_xlsx
from app.docgen.genstore import parse_spec
from app.security.masking import mask_text
from app.security.url_guard import UrlBlocked, resolve_and_validate
from app.tools import CAPABILITY_TOOLS
from app.tools.web import fetch_url, search_web
from app.cache.tool_cache import is_cacheable_tool_name

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


async def check_url_guard() -> None:
    """SSRF 拒绝面: 每一条都必须被拒; 公网 IP 字面量必须放行(不依赖 DNS)。"""
    blocked = [
        "http://169.254.169.254/latest/meta-data/",   # 云元数据(链路本地保留段)
        "http://localhost/admin",
        "http://LOCALHOST:8000/api",                   # 大小写不变体
        "http://postgres:5432/",                       # compose 服务名(内网拓扑探测)
        "http://host.docker.internal:11434/",
        "http://127.0.0.1:6379/",
        "http://10.1.2.3/internal",                    # 私网字面量
        "http://192.168.1.10/router",
        "http://172.20.0.5:9000/",
        "ftp://example.com/file",                      # 非 http(s)
        "file:///etc/passwd",
        "http://user:pass@example.com/",               # userinfo 混淆
        "http://foo.localhost/",                       # 保留后缀变体
        "http://svc.internal/",
    ]
    for url in blocked:
        try:
            await resolve_and_validate(url)
            check(f"SSRF 拒绝 {url}", False, "未被拒绝")
        except UrlBlocked as exc:
            check(f"SSRF 拒绝 {url}", bool(exc.reason), "")
        except Exception as exc:  # noqa: BLE001
            check(f"SSRF 拒绝 {url}", False, f"{type(exc).__name__}: {exc}")

    for url in ("https://1.1.1.1/", "http://8.8.8.8/dns-query"):
        try:
            ips = await resolve_and_validate(url)
            check(f"SSRF 放行公网 IP {url}", bool(ips), "")
        except Exception as exc:  # noqa: BLE001
            check(f"SSRF 放行公网 IP {url}", False, f"{type(exc).__name__}: {exc}")

    # 白名单语义(计划 C1): 启用后 default-deny, 子域放行。
    from app.security.url_guard import _host_allowed_by_allowlist

    allow = ["example.com"]
    check("白名单放行主域", _host_allowed_by_allowlist("example.com", allow))
    check("白名单放行子域", _host_allowed_by_allowlist("api.example.com", allow))
    check("白名单拒绝外域", not _host_allowed_by_allowlist("evil.com", allow))
    check("白名单拒绝伪装后缀", not _host_allowed_by_allowlist("notexample.com", allow))


async def check_tools_surface() -> None:
    check("能力注册表覆盖 web/docgen", set(CAPABILITY_TOOLS) == {"web", "docgen"}, str(set(CAPABILITY_TOOLS)))
    check(
        "web 域工具齐备",
        {t.name for t in CAPABILITY_TOOLS["web"]} == {"search_web", "fetch_url"},
        str([t.name for t in CAPABILITY_TOOLS["web"]]),
    )
    check(
        "docgen 域工具齐备(生成 + 并入只读检索)",
        {t.name for t in CAPABILITY_TOOLS["docgen"]}
        == {"generate_docx", "generate_xlsx", "generate_pptx", "generate_pdf", "generate_md", "generate_image",
            "search_web", "fetch_url"},
        str([t.name for t in CAPABILITY_TOOLS["docgen"]]),
    )
    # 命名即缓存策略(计划摘要/被拒方案 5): 检索可缓存, 抓取与生成不缓存。
    check("search_web 命中缓存白名单", is_cacheable_tool_name("search_web"))
    check("fetch_url 不缓存", not is_cacheable_tool_name("fetch_url"))
    check("generate_* 不缓存", not any(is_cacheable_tool_name(n) for n in ("generate_docx", "generate_xlsx", "generate_pptx", "generate_pdf", "generate_md", "generate_image")))

    # 联网检索载荷形状(验收第 3 项的离线部分): ddgs 可达则拿结果, 不可达必须降级而非抛。
    result = await search_web.ainvoke({"query": "reportlab 中文 字体", "max_results": 3})
    ok_shape = (
        "results" in result
        and isinstance(result["results"], list)
        and ("degraded" in result)
        and ("error" not in result or isinstance(result["error"], str))
    )
    check("search_web 返回结构化载荷", ok_shape, str(result)[:200])
    if result.get("results"):
        check("检索结果带来源 url", all(bool(r.get("url")) for r in result["results"]), str(result)[:200])
    else:
        check("检索不可达时显式降级", result.get("degraded") is True and bool(result.get("error")), str(result)[:200])
    empty = await search_web.ainvoke({"query": "   "})
    check("空检索词被拒且不抛", empty.get("degraded") is True and empty.get("results") == [])


async def check_fetch_guard() -> None:
    """fetch_url 的护栏拒绝面(验收第 4 项的工具侧入口)。"""
    for url in ("http://169.254.169.254/latest/meta-data/", "http://postgres:5432/", "http://localhost/"):
        result = await fetch_url.ainvoke({"url": url})
        check(f"fetch_url 拒绝 {url}", bool(result.get("error")) and "护栏" in str(result.get("error", "")), str(result)[:160])
    result = await fetch_url.ainvoke({"url": ""})
    check("fetch_url 空 url 被拒", bool(result.get("error")), str(result))


def check_builders(keep: bool) -> None:
    spec = {
        "title": "季度费用总结",
        "subtitle": "冒烟样例",
        "sections": [{"heading": "结论", "body": "费用环比上涨 12%。\n主要来自研发。"}],
        "bullets": ["要点一", "要点二"],
        "table": {"columns": ["部门", "金额"], "rows": [["研发", 42], ["市场", 18]]},
    }
    data, err = parse_spec(spec)
    check("spec 解析(dict 直通)", data is not None and not err, err)
    fenced, err = parse_spec('```json\n{"title": "x"}\n```')
    check("spec 解析容忍围栏", fenced is not None and not err, err)
    bad, err = parse_spec("这不是JSON")
    check("spec 解析拒绝非法输入", bad is None and bool(err), str(err))

    token = genstore.new_token()
    directory = genstore.gen_dir(token)
    built: list[Path] = []
    try:
        builders = {
            "docx": build_docx,
            "xlsx": build_xlsx,
            "pptx": build_pptx,
            "pdf": build_pdf,
            "md": build_md,
        }
        magics = {"docx": b"PK", "xlsx": b"PK", "pptx": b"PK", "pdf": b"%PDF", "md": b"# "}
        for kind, builder in builders.items():
            name = genstore.new_file_name(f".{kind}", spec["title"])
            path = directory / name
            builder(path, data)
            built.append(path)
            head = path.read_bytes()[:4]
            ok = head.startswith(magics[kind]) and path.stat().st_size > 200
            check(f"{kind} 构建成功且文件头正确", ok, f"head={head!r} size={path.stat().st_size}")
            # 生成物必须能被对应库重新打开(验收第 5 项的"不乱码/不损坏"最低门限)。
            if kind == "docx":
                from docx import Document as _D

                _D(str(path))
            elif kind == "xlsx":
                from openpyxl import load_workbook as _L

                _L(str(path))
            elif kind == "pptx":
                from pptx import Presentation as _P

                _P(str(path))
            elif kind == "pdf":
                head_text = path.read_bytes()[:1024]
                check("pdf 内嵌 CID 字体声明", b"STSong-Light" in head_text or b"STSong" in head_text, str(head_text[:80]))

        empty_pdf = directory / genstore.new_file_name(".pdf", "empty-smoke")
        try:
            build_pdf(empty_pdf, {"title": "", "sections": []})
            check("空 spec 被构建器拒绝", False, "未抛 ValueError")
        except ValueError:
            check("空 spec 被构建器拒绝", True)
            empty_pdf.unlink(missing_ok=True)
        # 注: 上面的用例文件名必须与 built 里的真实产物不同名 —— new_file_name 秒级时间戳
        # + 中文标题无 slug 时, 同秒同名会互相覆盖/unlink 掉样例文件(这是本脚本踩过的坑)。

        # 令牌/穿越防护(验收第 4 项的路径面)
        check("非法令牌被拒", genstore.build_path("reports", "x.pdf") is None)
        check("穿越文件名被拒", genstore.build_path(token, "../../secret.txt") is None)
        check("不存在文件被拒", genstore.build_path(token, "nope.pdf") is None)
        check("合法产物可定位", genstore.build_path(token, built[3].name) is not None if len(built) > 3 else False)

        # 保留期清扫(计划 E4): 把目录 mtime 拨回 2 天前再清扫。
        if not keep:
            age = time.time() - 48 * 3600
            os.utime(directory, (age, age))
            removed = genstore.cleanup_expired()
            check("过期生成目录被清扫", removed >= 1 and not directory.exists(), f"removed={removed}")
            built = []
    finally:
        if not keep and directory.exists():
            shutil.rmtree(directory, ignore_errors=True)


def check_capability_routing() -> None:
    """意图规则层回归(验收第 8 项): 新增能力域不得挤歪既有路由。"""
    recognizer = object.__new__(IntentRecognizer)  # 规则层是纯函数, 不构造 LLM

    def rule(text: str):
        return recognizer._rule_classify(text)

    cases = [
        ("搜一下今天的行业新闻", IntentType.TOOL_CALL, "web"),
        ("帮我搜最近的 AI 进展", IntentType.TOOL_CALL, "web"),
        ("把报销明细导出成excel", IntentType.TOOL_CALL, "docgen"),
        ("生成一份季度总结的word文档", IntentType.TOOL_CALL, "docgen"),
        ("做一份汇报PPT", IntentType.TOOL_CALL, "docgen"),
    ]
    for text, intent, target in cases:
        got = rule(text)
        ok = got is not None and got.intent is intent and got.target == target
        check(f"规则层判 {intent.value}/{target}: {text}", ok, "" if ok else f"got={got}")

    regression = [
        ("我要报销", IntentType.AGENT_DELEGATE, "finance"),
        ("查询 FIN5000 报销单", IntentType.TOOL_CALL, "finance"),
        ("生成本周经营周报", IntentType.AGENT_DELEGATE, "analytics"),
        ("把这份报告导出成 Markdown", IntentType.TOOL_CALL, "docgen"),
        # 既有行为基线: "费用"关键词先于 analytics 命中(改动前后一致, 计划验收第 8 项只要求不变)。
        ("生成月度费用报表", IntentType.AGENT_DELEGATE, "finance"),
        ("查询 HR2001 工单", IntentType.TOOL_CALL, "hr"),
    ]
    for text, intent, target in regression:
        got = rule(text)
        ok = got is not None and got.intent is intent and got.target == target
        check(f"回归不变: {text}", ok, "" if ok else f"got={got}")

    fallback = recognizer._fallback("最新新闻")
    check("兜底层判 web", fallback.intent is IntentType.TOOL_CALL and fallback.target == "web", str(fallback))
    fallback = recognizer._fallback("帮我导出一份表格")
    check("兜底层判 docgen", fallback.intent is IntentType.TOOL_CALL and fallback.target == "docgen", str(fallback))


def check_masking_and_routes() -> None:
    """mask_text 不得损坏下载链接(计划验收第 6 项), 且保留原有打码能力。"""
    token = "1" * 32  # 全数字 hex: 最容易凑成 16 位"银行卡"位数的极端样例
    url = f"/api/files/{token}/docgen-20260928-120000-abc.docx"
    masked = mask_text(f"文件已生成: {url} 请下载")
    check("相对下载链接不被打码", url in masked, masked)
    abs_url = f"http://192.168.1.10:18000/api/files/{token}/x.pdf"
    check("绝对下载链接不被打码", abs_url in mask_text(f"链接 {abs_url}"))
    check("普通银行卡仍被打码", "****" in mask_text("卡号 6222020200112233456 请查收"))
    check("普通手机号仍被打码", "****" in mask_text("联系 13812345678"))

    from app.main import create_app

    paths = set(create_app().openapi()["paths"])
    check("下载路由已挂网关", "/api/files/{token}/{file_name}" in paths, "")
    check("网页成品回取端点仍在", "/api/files/reports/{name}" in paths, "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="进程内 web/docgen 工具离线冒烟")
    parser.add_argument("--keep", action="store_true", help="保留本次生成的产物文件")
    args = parser.parse_args(argv)

    print(f"生成物目录: {genstore.gen_root()}\n")

    asyncio.run(check_url_guard())
    asyncio.run(check_tools_surface())
    asyncio.run(check_fetch_guard())
    check_builders(args.keep)
    check_capability_routing()
    check_masking_and_routes()

    failed = [r for r in _results if not r[0]]
    print(f"\n合计 {len(_results)} 项: 通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    for _ok, name, detail in failed:
        print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
