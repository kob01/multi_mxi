"""图表渲染: 纯 Python SVG(bar / line / pie) + Pillow PNG(同一套数据的位图)。

为什么不引 matplotlib(本模块存在的首要理由):
- Dockerfile 用 ``uv sync --no-dev`` 装依赖, 加一个 30MB+ 的绘图库会拖慢每次镜像
  重建, 而这里只需要三类统计图;
- 浏览器侧首选 SVG: 文字交给浏览器用系统字体渲染, 中文天然正确, 矢量放大不糊,
  且 SVG 是文本, 能直接落进 reports 目录由网关静态回文件, 无需二进制通道;
- PNG 只给"图要进 office 文档"的场景(docx/pptx/pdf 嵌不了 SVG), 用已是既有依赖的
  Pillow 直接画 —— 详见下方 PNG 小节, 中文字体缺字时退默认字体而不抛异常。

设计取舍:
- 只做"数据图", 不做装饰: 无图例交互、无网格动画, 坐标轴 + 数值标签够用;
- 输入容错优先于报错: 单个 series/categories 缺失、值为 str/None 都归一为 0 并在
  返回里说明, 让 LLM 拿到"图已生成"而不是一个让它重试的异常。
"""

from __future__ import annotations

import html
import io
import logging
import math
import xml.etree.ElementTree as ET
from typing import Any, Sequence

_logger = logging.getLogger(__name__)

# 画布与留白: 左边界要容纳 y 轴刻度, 下边界要容纳可能旋转的 x 轴标签。
_WIDTH = 720
_HEIGHT = 420
_MARGIN = {"top": 44, "right": 24, "bottom": 76, "left": 72}

# 多系列配色(色盲友好序): 超过系列数循环取色。
_PALETTE = ("#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#AF7AA1", "#9C755F")

# 字体栈只允许单引号: 它会被写进双引号包裹的 font-family 属性里, 出现双引号会直接
# 把整张 SVG 的属性截断(浏览器渲染出一堆裸文本)。
_FONT = "system-ui, -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif"

# 引号在这里固化: SVG 按 XML 解析, XML 1.0 的 AttValue 强制带引号, 而 _FONT 含空格与
# 单引号 —— 裸写 font-family={_FONT} 会让整张图变成非良构 XML(浏览器直接报解析错,
# 图打不开)。所有写 font-family 的地方一律复用这个常量, 不要再手抄属性。
_FONT_ATTR = f'font-family="{_FONT}"'


def _text_width(label: str) -> float:
    """粗略估算文本渲染宽度(px): CJK 按 1 字 1 个全角宽, ASCII 按 0.55 折算。

    只用于判断 x 轴标签是否需要旋转, 不参与布局精算 —— 估不准只会多旋转一次,
    不会把图画坏。
    """
    units = sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in str(label))
    return units * 12


def _to_number(value: Any) -> float:
    """把 LLM/DB 可能给出的各种数值形态收敛成 float; 收不住按 0 处理。"""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def fmt(value: float, digits: int = 2) -> str:
    """数值标签: 整数值不带小数点, 大额用万做单位(中文报表习惯)。"""
    if value == int(value) and abs(value) < 1e8:
        return f"{int(value):,}"
    if abs(value) >= 1e8:
        return f"{value / 1e8:.{digits}f}亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.{digits}f}万"
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".")


def _nice_ticks(hi: float, count: int = 5) -> tuple[float, float, float]:
    """从 0 起步的"好看"刻度: 返回 (lo, hi, step), 步长取 1/2/5×10^k。"""
    if not math.isfinite(hi) or hi <= 0:
        return 0.0, 1.0, 0.25
    raw = hi / count
    exponent = math.floor(math.log10(raw)) if raw > 0 else 0
    base = 10.0**exponent
    for factor in (1, 2, 2.5, 5, 10):
        step = factor * base
        if step * count >= hi:
            return 0.0, step * count, step
    return 0.0, step * count, step


def normalize(
    categories: Sequence[Any],
    series: Sequence[Any] | None,
    values: Sequence[Any] | None,
) -> tuple[list[str], list[dict[str, Any]], int]:
    """把两种常见入参形态统一成 (categories, [{name, data}], dropped)。

    形态 A(推荐): categories=["研发部",...], series=[{"name":"金额","data":[...]}]
    形态 B(省 token): categories=[["研发部", 1200], ...] 或 values=[1200, ...]
    容错: 长度不齐按短的对齐, 非数值转 0 —— 都计入 dropped 供调用方说明。
    """
    dropped = 0
    cats: list[str] = [str(c) for c in (categories or [])]
    out: list[dict[str, Any]] = []

    if series:
        for item in series:
            if isinstance(item, dict):
                name = str(item.get("name") or "数值")
                data = [_to_number(v) for v in (item.get("data") or [])]
            else:
                name, data = "数值", [_to_number(v) for v in (item or [])]
            if not cats and len(data) == len(cats):
                cats = [f"#{i + 1}" for i in range(len(data))]
            dropped += max(0, len(cats) - len(data))
            out.append({"name": name, "data": data[: len(cats)]})
        return cats, out, dropped

    # 形态 B: categories 里可能是 [label, value] 成对, 或直接由 values 给数值
    if values:
        dropped += max(0, len(cats) - len(values))
        out.append({"name": "数值", "data": [_to_number(v) for v in values][: len(cats)]})
    elif cats and all(isinstance(c, (list, tuple)) and len(c) >= 2 for c in cats):
        pairs = list(cats)
        cats = [str(p[0]) for p in pairs]
        out.append({"name": "数值", "data": [_to_number(p[1]) for p in pairs]})
    elif cats:
        # 只剩一串裸值: 当作单系列, 用序号当类目(至少图能出来)
        values_only = [_to_number(c) for c in cats]
        cats = [f"#{i + 1}" for i in range(len(values_only))]
        out.append({"name": "数值", "data": values_only})
    return cats, out, dropped


def _axis(chart: dict[str, Any], title: str) -> list[str]:
    """画 y 轴刻度线 + 轴标题(共用部分, 便于 bar/line 复用同一套坐标)。"""
    x0, y0 = _MARGIN["left"], _MARGIN["top"]
    x1 = _WIDTH - _MARGIN["right"]
    y1 = _HEIGHT - _MARGIN["bottom"]
    lo, hi, step = chart["lo"], chart["hi"], chart["step"]
    plot_h = y1 - y0
    parts: list[str] = []
    ticks = 0
    value = lo
    while value <= hi + 1e-9 and ticks < 12:
        y = y1 - (value - lo) / (hi - lo or 1) * plot_h
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" '
            f'stroke="#E7EBF0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="12" '
            f'fill="#6B7280" {_FONT_ATTR}>{html.escape(fmt(value, 1))}</text>'
        )
        value += step
        ticks += 1
    parts.append(
        f'<text x="{x0}" y="{_MARGIN["top"] - 18}" font-size="15" fill="#111827" '
        f'font-weight="600" {_FONT_ATTR}>{html.escape(title)}</text>'
    )
    parts.append(
        f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#9AA4B2" stroke-width="1"/>'
    )
    return parts


def _category_labels(chart: dict[str, Any]) -> list[str]:
    """x 轴类目标签; 拥挤或过长时整体旋转 -35°(旋转比截断更不容易丢信息)。"""
    cats = chart["cats"]
    x0, y1 = _MARGIN["left"], _HEIGHT - _MARGIN["bottom"]
    slot = (chart["x1"] - x0) / max(1, len(cats))
    need_rotate = any(_text_width(c) > slot - 6 for c in cats) or len(cats) > 8
    parts: list[str] = []
    for i, cat in enumerate(cats):
        cx = x0 + slot * (i + 0.5)
        label = cat if len(cat) <= 14 else cat[:13] + "…"
        if need_rotate:
            parts.append(
                f'<text x="{cx:.1f}" y="{y1 + 16}" font-size="12" fill="#4B5563" '
                f'text-anchor="end" {_FONT_ATTR} '
                f'transform="rotate(-35 {cx:.1f} {y1 + 16})">{html.escape(label)}</text>'
            )
        else:
            parts.append(
                f'<text x="{cx:.1f}" y="{y1 + 20}" font-size="12" fill="#4B5563" '
                f'text-anchor="middle" {_FONT_ATTR}>{html.escape(label)}</text>'
            )
    return parts


def _legend(chart: dict[str, Any]) -> list[str]:
    """多系列才画图例(顶部一行); 单系列的名称已在标题里, 再画一遍是噪声。"""
    series = chart["series"]
    if len(series) < 2:
        return []
    parts: list[str] = []
    x = _MARGIN["left"]
    y = _HEIGHT - 22
    for i, s in enumerate(series):
        color = _PALETTE[i % len(_PALETTE)]
        parts.append(f'<rect x="{x}" y="{y - 9}" width="12" height="12" fill="{color}"/>')
        label = s["name"] if len(s["name"]) <= 12 else s["name"][:11] + "…"
        parts.append(
            f'<text x="{x + 17}" y="{y + 1}" font-size="12" fill="#4B5563" '
            f'{_FONT_ATTR}>{html.escape(label)}</text>'
        )
        x += 24 + _text_width(label)
    return parts


def _wrap(parts: list[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" '
        f'viewBox="0 0 {_WIDTH} {_HEIGHT}" {_FONT_ATTR} role="img">'
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="#FFFFFF"/>'
        + "".join(parts)
        + "</svg>"
    )


def _scale(cats: list[str], series: list[dict[str, Any]]) -> dict[str, Any]:
    """共用坐标计算: 类目数、系列对齐、y 轴好看刻度。"""
    aligned = [list(s["data"]) + [0.0] * (len(cats) - len(s["data"])) for s in series]
    flat = [v for row in aligned for v in row]
    hi = max(flat) if flat else 1.0
    lo, top, step = _nice_ticks(hi)
    return {
        "cats": cats,
        "series": [dict(s, data=d) for s, d in zip(series, aligned)],
        "lo": lo,
        "hi": top,
        "step": step,
        "x0": _MARGIN["left"],
        "x1": _WIDTH - _MARGIN["right"],
        "y0": _MARGIN["top"],
        "y1": _HEIGHT - _MARGIN["bottom"],
    }


def bar_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
    """柱状图(支持分组多系列): 部门对比、类别分布这类"离散类目 × 数值"首选。"""
    chart = _scale(categories, series)
    plot_h = chart["y1"] - chart["y0"]
    slot = (chart["x1"] - chart["x0"]) / max(1, len(chart["cats"]))
    group_w = slot * 0.72
    bar_w = group_w / max(1, len(chart["series"]))
    parts = _axis(chart, title) + _category_labels(chart)
    for si, s in enumerate(chart["series"]):
        color = _PALETTE[si % len(_PALETTE)]
        for ci, value in enumerate(s["data"]):
            x = chart["x0"] + slot * ci + (slot - group_w) / 2 + bar_w * si
            h = max(0.0, (value - chart["lo"]) / (chart["hi"] - chart["lo"] or 1) * plot_h)
            y = chart["y1"] - h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(1.0, bar_w - 2):.1f}" '
                f'height="{h:.1f}" fill="{color}" rx="2"/>'
            )
            # 单系列且空间够时把数值写在柱顶; 多系列只写第一系列避免叠字
            if si == 0 or len(chart["series"]) == 1:
                parts.append(
                    f'<text x="{x + (bar_w - 2) / 2:.1f}" y="{y - 5:.1f}" font-size="11" '
                    f'fill="#374151" text-anchor="middle" {_FONT_ATTR}>'
                    f"{html.escape(fmt(value, 1))}</text>"
                )
    parts += _legend(chart)
    return _wrap(parts)


def line_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
    """折线图: 时间序列(按月费用趋势、按周工单量)。点多时省略部分数值标签防糊。"""
    chart = _scale(categories, series)
    plot_h = chart["y1"] - chart["y0"]
    n = max(1, len(chart["cats"]))
    step_x = (chart["x1"] - chart["x0"]) / n
    parts = _axis(chart, title) + _category_labels(chart)
    for si, s in enumerate(chart["series"]):
        color = _PALETTE[si % len(_PALETTE)]
        points = [
            (chart["x0"] + step_x * (i + 0.5), chart["y1"] - (v - chart["lo"]) / (chart["hi"] - chart["lo"] or 1) * plot_h)
            for i, v in enumerate(s["data"])
        ]
        if len(points) > 1:
            path = " ".join(
                f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points)
            )
            parts.append(
                f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5" '
                f'stroke-linejoin="round" stroke-linecap="round"/>'
            )
        label_every = max(1, math.ceil(len(points) / 12))
        for i, (x, y) in enumerate(points):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>')
            if i % label_every == 0:
                parts.append(
                    f'<text x="{x:.1f}" y="{y - 9:.1f}" font-size="11" fill="#374151" '
                    f'text-anchor="middle" {_FONT_ATTR}>'
                    f"{html.escape(fmt(s['data'][i], 1))}</text>"
                )
    parts += _legend(chart)
    return _wrap(parts)


def pie_chart(title: str, categories: list[str], series: list[dict[str, Any]]) -> str:
    """饼图: 构成占比(类别费用占比)。只取前 8 片, 其余归"其他", 小片不写标签。"""
    data = series[0]["data"] if series and series[0]["data"] else [0.0]
    pairs = list(zip(categories or ["" for _ in data], data))[: max(1, len(data))]
    pairs = [(c, max(0.0, v)) for c, v in pairs]
    total = sum(v for _, v in pairs) or 1.0
    if len(pairs) > 8:
        head, tail = pairs[:7], pairs[7:]
        pairs = head + [("其他", sum(v for _, v in tail))]
    cx, cy, r = _MARGIN["left"] + 150, _HEIGHT / 2 + 6, 128
    parts = [
        f'<text x="{_MARGIN["left"]}" y="{_MARGIN["top"] - 18}" font-size="15" '
        f'fill="#111827" font-weight="600" {_FONT_ATTR}>{html.escape(title)}</text>'
    ]
    angle = -math.pi / 2
    for i, (cat, value) in enumerate(pairs):
        share = value / total
        sweep = share * math.pi * 2
        x0 = cx + r * math.cos(angle)
        y0 = cy + r * math.sin(angle)
        x1 = cx + r * math.cos(angle + sweep)
        y1 = cy + r * math.sin(angle + sweep)
        large = 1 if sweep > math.pi else 0
        color = _PALETTE[i % len(_PALETTE)]
        if len(pairs) == 1:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
        else:
            parts.append(
                f'<path d="M{cx:.1f},{cy:.1f} L{x0:.1f},{y0:.1f} '
                f'A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f} Z" fill="{color}" '
                f'stroke="#FFFFFF" stroke-width="1.5"/>'
            )
        mid = angle + sweep / 2
        if share >= 0.04:
            lx = cx + (r * 0.62) * math.cos(mid)
            ly = cy + (r * 0.62) * math.sin(mid)
            parts.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="12" fill="#FFFFFF" '
                f'text-anchor="middle" {_FONT_ATTR}>{share * 100:.1f}%</text>'
            )
        angle += sweep
    # 右侧图例: 类目名 + 金额, 饼图不放图例等于只画了半张图
    ly = cy - r + 12
    for i, (cat, value) in enumerate(pairs):
        color = _PALETTE[i % len(_PALETTE)]
        x = cx + r + 34
        parts.append(f'<rect x="{x}" y="{ly - 9}" width="11" height="11" fill="{color}"/>')
        label = cat if len(cat) <= 10 else cat[:9] + "…"
        parts.append(
            f'<text x="{x + 16}" y="{ly}" font-size="12" fill="#4B5563" '
            f'{_FONT_ATTR}>{html.escape(label)} · {html.escape(fmt(value, 1))}</text>'
        )
        ly += 20
    return _wrap(parts)


def _well_formed(svg: str) -> str | None:
    """出图前的良构 XML 自检; 有问题返回错因, 干净返回 None。

    这层不是可有可无的装饰: SVG 以 ``image/svg+xml`` 下发时浏览器按 XML 解析,
    一个未加引号的属性就会让整张图变成解析错误页(而工具返回值里仍写着"已生成")。
    手拼字符串没有编译器兜底, 只能用进出的这一道把这类回归顶出来。
    """
    try:
        ET.fromstring(svg)
    except ET.ParseError as exc:
        return f"SVG 非良构 XML({exc})"
    return None


def render(
    chart_type: str,
    title: str,
    categories: Sequence[Any],
    series: Sequence[Any] | None = None,
    values: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """统一入口: 归一入参 -> 选图型 -> 产出 SVG 文本。

    Returns:
        ``{svg, chart_type, categories_count, series_names, dropped}``;
        图型不认识或无数据时返回 ``{error}`` 让调用方( ReAct 循环 )自行改参。
    """
    kind = (chart_type or "bar").strip().lower()
    cats, ser, dropped = normalize(categories, series, values)
    if not cats or not ser:
        return {"error": "图表无数据: 请提供 categories 与 series(或 values)"}
    if kind not in ("bar", "line", "pie"):
        return {"error": f"不支持的图表类型: {kind}; 可选 bar/line/pie"}
    if kind == "pie":
        svg = pie_chart(title or "占比分布", cats, ser)
    elif kind == "line":
        svg = line_chart(title or "趋势", cats, ser)
    else:
        svg = bar_chart(title or "对比", cats, ser)
    bad = _well_formed(svg)
    if bad:
        # 到这里只能是本模块的拼接 bug(不是用户传参问题), 记日志后让调用方自行降级。
        _logger.error("chart svg rejected: %s | kind=%s title=%s", bad, kind, title)
        return {"error": f"图表生成异常, 已拦截: {bad}"}
    return {
        "svg": svg,
        "chart_type": kind,
        "categories_count": len(cats),
        "series_names": [s["name"] for s in ser],
        "dropped_points": dropped,
    }


# ---------------------------------------------------------------------------
# PNG 渲染(与上面同一套数据, 用 Pillow 直接画)
# ---------------------------------------------------------------------------
# 为什么还要一份 PNG: Word/PPT/PDF 里嵌不了 SVG(python-docx 需 png 兜底、reportlab
# 不认 SVG), 而分析图要能进 office 文档就得有位图。刻意不引 matplotlib/cairosvg:
# 前者太重、后者要系统 cairo —— 这里只需要三类图, Pillow(已是既有依赖)够用。
# 中文靠探测系统里现成的 CJK 字体(Windows 有微软雅黑/黑体; Linux 常见文泉驿/Noto);
# 找不到时退回 PIL 默认字体(ASCII 能显示, 中文标签会退化), 但仍**不抛** —— 出图优先。

_PNG_W, _PNG_H = _WIDTH, _HEIGHT
_FONT_CACHE: dict[int, Any] = {}


def _load_font(size: int):
    """按字号取一个尽量支持 CJK 的字体; 进程内缓存, 找不到就退默认字体。"""
    from PIL import ImageFont

    hit = _FONT_CACHE.get(size)
    if hit is not None:
        return hit
    candidates = (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/PingFang.ttc",
    )
    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:  # noqa: BLE001
            font = None
    _FONT_CACHE[size] = font
    return font


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except (ValueError, IndexError):
        return (0, 0, 0)


def _truncate(draw: Any, text: str, font: Any, max_px: int) -> str:
    text = str(text)
    if max_px <= 0 or not font:
        return text
    while text and draw.textlength(text, font=font) > max_px:
        text = text[:-1]
    return text


def render_png(
    chart_type: str,
    title: str,
    categories: Sequence[Any],
    series: Sequence[Any] | None = None,
    values: Sequence[Any] | None = None,
    background: str = "#FFFFFF",
) -> dict[str, Any]:
    """与 :func:`render` 同数据同图型, 但产出 PNG 字节(供 office 内嵌)。

    Returns:
        ``{png: bytes, chart_type, categories_count}``; 无数据/图型不支持返回 ``{error}``。
        Pillow 不可用时也返回 ``{error}`` 而不是抛出(调用方据此只出 SVG)。
    """
    kind = (chart_type or "bar").strip().lower()
    if kind not in ("bar", "line", "pie"):
        return {"error": f"不支持的图表类型: {kind}; 可选 bar/line/pie"}
    cats, ser, _ = normalize(categories, series, values)
    if not cats or not ser:
        return {"error": "图表无数据: 请提供 categories 与 series(或 values)"}
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return {"error": "Pillow 不可用, 无法出 PNG"}

    img = Image.new("RGB", (_PNG_W, _PNG_H), _rgb(background))
    draw = ImageDraw.Draw(img)
    f_title, f_axis, f_small = _load_font(16), _load_font(12), _load_font(11)
    x0, y0 = _MARGIN["left"], _MARGIN["top"]
    x1, y1 = _WIDTH - _MARGIN["right"], _HEIGHT - _MARGIN["bottom"]
    draw.text((x0, 12), (title or "图表")[:60], fill=(17, 24, 39), font=f_title)

    aligned = [list(s["data"]) + [0.0] * (len(cats) - len(s["data"])) for s in ser]
    flat = [v for row in aligned for v in row]
    lo, hi, step = _nice_ticks(max(flat) if flat else 1.0)
    hi = hi or 1.0
    plot_h, plot_w = y1 - y0, x1 - x0

    def y_of(v: float) -> float:
        return y1 - (v - lo) / (hi - lo or 1) * plot_h

    # y 轴刻度 + 网格
    val, guard = lo, 0
    while val <= hi + 1e-9 and guard < 12:
        y = y_of(val)
        draw.line([(x0, y), (x1, y)], fill=(231, 235, 240), width=1)
        draw.text((x0 - 8, y - 6), fmt(val, 1), fill=(107, 114, 128), font=f_small)
        val += step
        guard += 1
    draw.line([(x0, y1), (x1, y1)], fill=(154, 164, 178), width=1)

    n = max(1, len(cats))
    slot = plot_w / n
    if kind == "pie":
        _png_pie(draw, cats, aligned[0], f_axis, f_small)
    elif kind == "line":
        for si, data in enumerate(aligned):
            color = _rgb(_PALETTE[si % len(_PALETTE)])
            pts = [(x0 + slot * (i + 0.5), y_of(v)) for i, v in enumerate(data)]
            if len(pts) > 1:
                draw.line(pts, fill=color, width=3)
            for px, py in pts:
                draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=color)
        _png_cat_labels(draw, cats, slot, x0, y1, f_small)
    else:
        group = slot * 0.72
        bar = group / max(1, len(aligned))
        for si, data in enumerate(aligned):
            color = _rgb(_PALETTE[si % len(_PALETTE)])
            for ci, v in enumerate(data):
                bx = x0 + slot * ci + (slot - group) / 2 + bar * si
                top = y_of(v)
                draw.rectangle([bx, top, bx + bar - 2, y1], fill=color)
                if len(aligned) == 1:
                    draw.text((bx + bar / 2 - 8, top - 14), fmt(v, 1), fill=(55, 65, 81), font=f_small)
        _png_cat_labels(draw, cats, slot, x0, y1, f_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"png": buf.getvalue(), "chart_type": kind, "categories_count": len(cats)}


def _png_cat_labels(draw: Any, cats: list[str], slot: float, x0: float, y1: float, font: Any) -> None:
    for i, cat in enumerate(cats):
        cx = x0 + slot * (i + 0.5)
        label = _truncate(draw, str(cat), font, max(12, slot - 6))
        draw.text((cx - draw.textlength(label, font=font) / 2, y1 + 8), label, fill=(75, 85, 99), font=font)


def _png_pie(draw: Any, cats: list[str], data: list[float], font: Any, small: Any) -> None:
    total = sum(max(0.0, v) for v in data) or 1.0
    cx, cy, r = _MARGIN["left"] + 150, _HEIGHT / 2 + 6, 128
    start = 0.0
    pairs = list(zip(cats, [max(0.0, v) for v in data]))
    for i, (cat, v) in enumerate(pairs):
        extent = v / total * 360.0
        color = _rgb(_PALETTE[i % len(_PALETTE)])
        if len(pairs) == 1:
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        else:
            draw.pieslice([cx - r, cy - r, cx + r, cy + r], start, start + extent, fill=color, outline=(255, 255, 255))
        start += extent
    ly = cy - r
    lx = cx + r + 24
    for i, (cat, v) in enumerate(pairs):
        color = _rgb(_PALETTE[i % len(_PALETTE)])
        draw.rectangle([lx, ly, lx + 11, ly + 11], fill=color)
        draw.text((lx + 16, ly - 2), f"{_truncate(draw, str(cat), small, 120)} · {fmt(v, 1)}", fill=(75, 85, 99), font=small)
        ly += 20
