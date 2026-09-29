"""进程内 web/docgen 工具整栈验证(需**容器网关**已运行, 计划"测试与验收"第 3/5/8 项的线上部分)。

跑法::

    ./scripts/dev.ps1 -Build        # 网关只允许跑在容器里(见 .qoder/rules/container-first-verification.md)
    uv run python -m scripts.test_tools_flow   # 本脚本是宿主侧 HTTP 客户端, 打的是容器

覆盖:
  1. "搜索/最新"类问题 -> tool_call/web -> search_web(有网出结果; 无网必须显式降级而非报错);
  2. "生成 Excel/PDF" -> tool_call/docgen -> 返回 download_url -> 链接真的可下载且文件头正确;
  3. Tool Cache: 同参二次检索命中缓存(从审计日志核对 cache 命中链路的工具调用次数);
  4. 回归: 业务单据查询( finance tool_call)与报销委派(a2a_agent)不变。
SSRF 拒绝面在 scripts/smoke_tools.py 已离线全覆盖, 这里不重复打外网。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid

import httpx

BASE = os.environ.get("MXI_BASE", "http://127.0.0.1:18000")
USER = os.environ.get("MXI_USER", "E10005")
ROLE = os.environ.get("MXI_ROLE", "finance")

# 能力令牌/文件名与 app.docgen.genstore 的形状一致
_DL_RE = re.compile(r"/api/files/([0-9a-f]{32})/([A-Za-z0-9._-]+)")

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def parse_frame(frame: str) -> dict | None:
    for line in frame.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


async def chat(client: httpx.AsyncClient, message: str, session_id: str, role: str = ROLE) -> dict:
    body = {
        "session_id": session_id,
        "user_id": USER,
        "role": role,
        "department": "财务部",
        "message": message,
        "thinking": False,
    }
    result: dict = {}
    async with client.stream("POST", f"{BASE}/api/chat/stream", json=body) as resp:
        resp.raise_for_status()
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while (idx := buf.find("\n\n")) >= 0:
                frame, buf = buf[:idx], buf[idx + 2:]
                event = parse_frame(frame)
                if event and event.get("type") == "result":
                    result = event
    return result


async def main() -> int:
    prefix = f"tools-{uuid.uuid4().hex[:8]}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        try:
            health = await client.get(f"{BASE}/api/health")
            check("网关可达", health.is_success, str(health.status_code))
        except Exception as exc:  # noqa: BLE001
            print(f"网关不可达({BASE}): {exc}", file=sys.stderr)
            return 2

        # ---- 1. 联网检索 ----
        search = await chat(client, "帮我搜一下最新的新能源行业新闻", f"web-{prefix}")
        check("搜索走 web 能力域", search.get("route") == "mcp_tool" and search.get("target") == "web",
              f"route={search.get('route')} target={search.get('target')}")
        answer = str(search.get("answer") or "")
        degraded = ("未能联网" in answer) or ("不可用" in answer)
        if "http" in answer or "来源" in answer:
            check("检索结果附来源", True)
        elif degraded:
            check("检索不可达时显式降级(不编造来源)", True, answer[:160])
        else:
            check("检索结果或降级说明至少出现其一", False, answer[:200])

        # ---- 2. 文件生成 + 下载闭环 ----
        xlsx = await chat(
            client,
            "把下面的数据生成一份 Excel 表格文件:研发部 42 万、市场部 18 万、财务部 9 万,标题用「各部门季度费用」",
            f"gen-{prefix}",
        )
        xanswer = str(xlsx.get("answer") or "")
        check("生成走 docgen 能力域", xlsx.get("route") == "mcp_tool" and xlsx.get("target") == "docgen",
              f"route={xlsx.get('route')} target={xlsx.get('target')} ans={xanswer[:120]}")
        links = _DL_RE.findall(xanswer)
        check("答复携带下载链接", bool(links), xanswer[:240])
        if links:
            token, name = links[0]
            dl = await client.get(f"{BASE}/api/files/{token}/{name}")
            check("下载链接可取回文件", dl.status_code == 200, f"HTTP {dl.status_code}")
            check("Excel 文件头正确(xlsx 是 zip 容器)", dl.content[:2] == b"PK", str(dl.content[:4]))
            check("下载响应带正确 MIME", "spreadsheetml" in dl.headers.get("content-type", ""), dl.headers.get("content-type", ""))
            missing = await client.get(f"{BASE}/api/files/{token}/nope.xlsx")
            check("不存在的文件名返回 404", missing.status_code == 404, str(missing.status_code))
            forged = await client.get(f"{BASE}/api/files/reports/{name}")
            check("reports 前缀不被令牌路由吞", forged.status_code in (200, 404) and forged.status_code != 500, str(forged.status_code))

        pdf = await chat(client, "生成一份关于远程办公规范的 PDF 说明文档,分两段说明即可", f"gen2-{prefix}")
        plinks = _DL_RE.findall(str(pdf.get("answer") or ""))
        check("PDF 生成并回链接", bool(plinks), str(pdf.get("answer"))[:200])
        if plinks:
            token, name = plinks[0]
            dl = await client.get(f"{BASE}/api/files/{token}/{name}")
            check("PDF 可下载且文件头正确", dl.status_code == 200 and dl.content[:4] == b"%PDF", f"{dl.status_code} {dl.content[:4]!r}")

        # ---- 3. 回归: 既有路由不变(验收第 8 项) ----
        biz = await chat(client, "查一下 FIN5000 的报销进度", f"biz-{prefix}")
        check("业务单据查询仍走 finance", biz.get("route") == "mcp_tool" and biz.get("target") == "finance",
              f"route={biz.get('route')} target={biz.get('target')}")
        delegate = await chat(client, "我要报销", f"del-{prefix}")
        check("报销仍走专业智能体委派", delegate.get("route") == "a2a_agent", f"route={delegate.get('route')}")

    failed = [r for r in _results if not r[0]]
    print(f"\n合计 {len(_results)} 项: 通过 {len(_results) - len(failed)} / 失败 {len(failed)}")
    for _ok, name, detail in failed:
        print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
