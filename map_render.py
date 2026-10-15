"""把「线路走向 + 实时车辆位置」渲染成一张 PNG 地图。

底图用高德公开的栅格瓦片（webrd0x.is.autonavi.com，无需 key，国内可直连），
线路走向来自 CMD=103 的 nihelist（道路拟合点），
车辆坐标来自 CMD=104 的 bus_lat / bus_lng / angle。

三个前提：
1. 只用 Pillow 画图，不引入地图 SDK —— AstrBot 运行时自带 Pillow。
2. 瓦片或中文字体缺失时优雅降级：保留线路与车辆，只是没有底图 / 站名。
3. 拿不到任何有效坐标时抛 ValueError，由调用方决定是否退化成纯文本推送。
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from functools import lru_cache
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("astrbot")

TILE_SIZE = 256
_TILE_URL = (
    "https://webrd0{s}.is.autonavi.com/appmaptile"
    "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
)
_TILE_SUBDOMAINS = ("1", "2", "3", "4")
_MIN_ZOOM = 9
_MAX_ZOOM = 17

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_ROUTE_COLOR = (24, 92, 235)
_ROUTE_CASING = (255, 255, 255)
_BUS_COLOR = (232, 62, 40)
_BUS_PASSED_COLOR = (250, 173, 20)
_BUS_IDLE_COLOR = (140, 146, 158)
_TARGET_COLOR = (8, 158, 82)

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\deng.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)


@lru_cache(maxsize=16)
def _cjk_font(size: int) -> ImageFont.FreeTypeFont | None:
    """找一个能显示中文的字体；一个都没有就返回 None（此时只画数字角标）。"""
    for path in _FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.ImageFont:
    return _cjk_font(size) or ImageFont.load_default()


# ---------------- 墨卡托投影 ----------------


def lonlat_to_world(lng: float, lat: float, zoom: int) -> tuple[float, float]:
    """经纬度 → 该缩放级别下的全局像素坐标（XYZ 瓦片约定，左上为原点）。"""
    scale = TILE_SIZE * (2**zoom)
    x = (lng + 180.0) / 360.0 * scale
    clamped = max(min(lat, 85.05112878), -85.05112878)
    y = (1.0 - math.asinh(math.tan(math.radians(clamped))) / math.pi) / 2.0 * scale
    return x, y


def _pick_zoom(bbox: tuple[float, float, float, float], width: int, height: int) -> int:
    """挑一个能把整条线路塞进画布的缩放级别（尽量放大）。"""
    min_lng, min_lat, max_lng, max_lat = bbox
    for zoom in range(_MAX_ZOOM, _MIN_ZOOM - 1, -1):
        left, top = lonlat_to_world(min_lng, max_lat, zoom)
        right, bottom = lonlat_to_world(max_lng, min_lat, zoom)
        if right - left <= width * 0.92 and bottom - top <= height * 0.92:
            return zoom
    return _MIN_ZOOM


# ---------------- 底图瓦片 ----------------


async def _get_tile(client: httpx.AsyncClient, zoom: int, x: int, y: int) -> bytes:
    host = _TILE_SUBDOMAINS[(x + y) % len(_TILE_SUBDOMAINS)]
    url = _TILE_URL.format(s=host, x=x, y=y, z=zoom)
    for _ in range(2):
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            continue
        if resp.status_code == 200 and resp.content:
            return resp.content
    return b""


async def _paste_tiles(
    image: Image.Image,
    client: httpx.AsyncClient,
    zoom: int,
    origin_x: float,
    origin_y: float,
    width: int,
    height: int,
) -> int:
    """把覆盖画布的瓦片贴到 image 上，返回成功贴上的张数。"""
    count = 2**zoom
    first_x = math.floor(origin_x / TILE_SIZE)
    first_y = math.floor(origin_y / TILE_SIZE)
    last_x = math.floor((origin_x + width - 1) / TILE_SIZE)
    last_y = math.floor((origin_y + height - 1) / TILE_SIZE)

    jobs = [
        (x, y)
        for y in range(first_y, last_y + 1)
        if 0 <= y < count
        for x in range(first_x, last_x + 1)
        if 0 <= x < count
    ]
    if not jobs:
        return 0
    raws = await asyncio.gather(*(_get_tile(client, zoom, x, y) for x, y in jobs))

    pasted = 0
    for (x, y), raw in zip(jobs, raws):
        if not raw:
            continue
        try:
            tile = Image.open(io.BytesIO(raw)).convert("RGB")
        except OSError:
            continue
        image.paste(
            tile,
            (
                round(x * TILE_SIZE - origin_x),
                round(y * TILE_SIZE - origin_y),
            ),
        )
        pasted += 1
    return pasted


# ---------------- 绘制工具 ----------------


def _text_center(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font,
    fill,
    stroke_fill=None,
    stroke_width: int = 0,
) -> None:
    """居中写字；不支持 anchor 的字体退回手动测量。"""
    kwargs = {"font": font, "fill": fill}
    if stroke_width:
        kwargs["stroke_fill"] = stroke_fill
        kwargs["stroke_width"] = stroke_width
    try:
        draw.text(xy, text, anchor="mm", **kwargs)
        return
    except (TypeError, ValueError):
        pass
    try:
        width = draw.textlength(text, font=font)
    except (AttributeError, TypeError):
        width = len(text) * 6
    size = getattr(font, "size", 10) or 10
    draw.text((xy[0] - width / 2, xy[1] - size / 2), text, **kwargs)


def _draw_bus(
    draw: ImageDraw.ImageDraw,
    font,
    x: float,
    y: float,
    angle: float | None,
    label: str,
    color: tuple[int, int, int],
    radius: int = 11,
) -> None:
    """画一辆车：先画车头指向的三角，再压上有编号的圆。"""
    if angle is not None and not math.isnan(angle):
        rad = math.radians(angle)
        dx, dy = math.sin(rad), -math.cos(rad)
        tip = (x + dx * (radius + 11), y + dy * (radius + 11))
        left = (
            x - dy * (radius + 7) + dx * (radius - 5),
            y + dx * (radius + 7) + dy * (radius - 5),
        )
        right = (
            x + dy * (radius + 7) + dx * (radius - 5),
            y - dx * (radius + 7) + dy * (radius - 5),
        )
        draw.polygon([tip, left, right], fill=color, outline=(255, 255, 255))

    draw.ellipse(
        [x - radius - 3, y - radius - 1, x + radius + 3, y + radius + 5],
        fill=(0, 0, 0, 55),
    )
    draw.ellipse(
        [x - radius, y - radius, x + radius, y + radius],
        fill=color,
        outline=(255, 255, 255),
        width=2,
    )
    if label:
        _text_center(draw, (x, y), label, font, (255, 255, 255))


def _draw_header(
    draw: ImageDraw.ImageDraw,
    title: str,
    subtitle: str,
    font_title,
    font_sub,
    width: int,
) -> None:
    lines = [text for text in (title, subtitle) if text]
    if not lines:
        return
    heights = [28, 20]
    box_h = 14 + sum(heights[: len(lines)])
    box_w = min(
        width - 20,
        26
        + max(
            draw.textlength(t, font=f) for t, f in zip(lines, (font_title, font_sub))
        ),
    )
    draw.rounded_rectangle(
        [10, 10, 10 + box_w + 16, 10 + box_h],
        radius=10,
        fill=(255, 255, 255, 228),
        outline=(198, 205, 216, 255),
    )
    y = 22
    for index, text in enumerate(lines):
        font = font_title if index == 0 else font_sub
        draw.text((22, y), text, font=font, fill=(28, 32, 40))
        y += heights[index]


# ---------------- 主入口 ----------------


async def render_route_map(
    route: dict,
    result: dict,
    station_order: int,
    title: str = "",
    *,
    width: int = 760,
    height: int = 560,
    with_tiles: bool = True,
    timeout: float = 6.0,
) -> bytes:
    """渲染线路地图，返回 PNG 字节。

    route: BusClient.get_route() 的返回值（含 stations / polyline）
    result: BusClient.query() 的返回值（含 buses / arrivals）
    """
    stations: list[dict] = route.get("stations") or []
    polyline: list[tuple[float, float]] = [
        (float(p[0]), float(p[1]))
        for p in (route.get("polyline") or [])
        if p and p[0] and p[1]
    ]
    station_points = [
        (float(s["lng"]), float(s["lat"]))
        for s in stations
        if s.get("lng") and s.get("lat")
    ]
    buses: list[dict] = [
        bus for bus in (result.get("buses") or []) if bus.get("lng") and bus.get("lat")
    ]

    path = polyline or station_points
    if not path:
        raise ValueError("这条线路没有可用的经纬度数据，无法画图")

    clouds = list(path) + station_points + [(b["lng"], b["lat"]) for b in buses]
    min_lng = min(p[0] for p in clouds)
    max_lng = max(p[0] for p in clouds)
    min_lat = min(p[1] for p in clouds)
    max_lat = max(p[1] for p in clouds)

    pad_lng = max((max_lng - min_lng) * 0.08, 0.0006)
    pad_lat = max((max_lat - min_lat) * 0.08, 0.0006)
    bbox = (min_lng - pad_lng, min_lat - pad_lat, max_lng + pad_lng, max_lat + pad_lat)

    zoom = _pick_zoom(bbox, width, height)
    left, top = lonlat_to_world(bbox[0], bbox[3], zoom)
    right, bottom = lonlat_to_world(bbox[2], bbox[1], zoom)
    origin_x = (left + right) / 2 - width / 2
    origin_y = (top + bottom) / 2 - height / 2

    def project(lng: float, lat: float) -> tuple[float, float]:
        wx, wy = lonlat_to_world(lng, lat, zoom)
        return wx - origin_x, wy - origin_y

    image = Image.new("RGB", (width, height), (238, 241, 245))
    if with_tiles:
        headers = {"User-Agent": _UA, "Referer": "https://www.amap.com/"}
        try:
            async with httpx.AsyncClient(
                timeout=timeout, headers=headers, follow_redirects=True
            ) as client:
                pasted = await _paste_tiles(
                    image, client, zoom, origin_x, origin_y, width, height
                )
            if pasted == 0:
                logger.debug("高德瓦片全部获取失败，退回纯色底图")
        except httpx.HTTPError as exc:
            logger.debug("获取地图瓦片失败，退回纯色底图：%s", exc)

    draw = ImageDraw.Draw(image, "RGBA")
    font_title = _font(17)
    font_sub = _font(13)
    font_label = _font(13)
    font_badge = _font(13)
    label_ok = _cjk_font(13) is not None

    # 线路
    points = [project(*item) for item in path]
    if len(points) > 1:
        draw.line(points, fill=_ROUTE_CASING, width=9, joint="curve")
        draw.line(points, fill=_ROUTE_COLOR, width=5, joint="curve")

    # 站点
    for station in stations:
        if not station.get("lng") or not station.get("lat"):
            continue
        if int(station.get("order") or 0) == station_order:
            continue
        x, y = project(float(station["lng"]), float(station["lat"]))
        if not (0 <= x <= width and 0 <= y <= height):
            continue
        draw.ellipse(
            [x - 3, y - 3, x + 3, y + 3],
            fill=(255, 255, 255),
            outline=_ROUTE_COLOR,
            width=2,
        )

    # 候车站（放大 + 站名气泡）
    target = next(
        (s for s in stations if int(s.get("order") or 0) == station_order), None
    )
    if target and target.get("lng") and target.get("lat"):
        x, y = project(float(target["lng"]), float(target["lat"]))
        draw.ellipse([x - 12, y - 12, x + 12, y + 12], fill=(8, 158, 82, 60))
        draw.ellipse(
            [x - 6, y - 6, x + 6, y + 6],
            fill=_TARGET_COLOR,
            outline=(255, 255, 255),
            width=2,
        )
        if label_ok:
            _draw_station_label(
                draw, font_label, x, y - 18, str(target.get("name") or "候车站")
            )

    # 车辆：1..N 与推送文案里的顺序一致，已过站的车用黄色区分
    ranks = {
        str(arrival.plate): index
        for index, arrival in enumerate(result.get("arrivals") or [], 1)
    }
    for index, bus in enumerate(buses):
        x, y = project(float(bus["lng"]), float(bus["lat"]))
        if not (0 <= x <= width and 0 <= y <= height):
            continue
        plate = str(bus.get("plate") or "")
        rank = ranks.get(plate)
        if rank is not None:
            color = _BUS_COLOR
            label = str(rank)
        elif bus.get("passed"):
            color = _BUS_PASSED_COLOR
            label = ""
        else:
            color = _BUS_IDLE_COLOR
            label = ""
        _draw_bus(draw, font_badge, x, y, bus.get("angle"), label, color)

    summary = f"在途 {len(buses)} 辆"
    if ranks:
        summary += f" · 候选 {len(ranks)} 辆（红色）"
    if result.get("data_time"):
        summary += f" · 数据 {result['data_time']}"
    _draw_header(draw, title, summary, font_title, font_sub, width)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_station_label(
    draw: ImageDraw.ImageDraw, font, x: float, y: float, text: str
) -> None:
    width = draw.textlength(text, font=font)
    box = [x - width / 2 - 6, y - 11, x + width / 2 + 6, y + 11]
    draw.rounded_rectangle(box, radius=8, fill=_TARGET_COLOR)
    _text_center(draw, (x, y), text, font, (255, 255, 255))
