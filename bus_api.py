"""掌上公交实时到站数据接口封装（全国通用）。

数据源：掌上公交 H5（https://h5.mygolbs.com/），协议逆向自其前端 js/mybus.js。

接口按 **城市名（CITYNAME）** 区分城市，不需要 citykey，
所以三明、福州、厦门、北京等都能查（个别城市未开通实时数据）。

要点：
1. 接口为 form-urlencoded 的 POST：https://h5.mygolbs.com/ApiData.do
2. **必须先 GET 首页预热会话**，否则服务端会返回一段固定密文而不是 JSON。
3. 必须携带 Referer / Origin / X-Requested-With，否则同样拿不到明文。
4. 所有请求通过 CMD 字段路由：
   - 101 全国城市列表（只返回城市名，共 500+ 个）
   - 204 城市校验（status=1 表示该城市可用）
   - 119 按关键字搜索线路（返回线路名 + 方向 + 起终点）
   - 103 线路详情（站点列表 + 首末班）
   - 104 实时到站（车辆列表 + 每段距离 + 每段速度）

「还有几站 / 约几分钟」的算法完全复刻自 js/mybus.js 的 getDistance / getCostTimes：

    order        = bus.index + 1                    # 1-based 车辆站序
    stops_away   = target_order - order             # 行驶中再 +1
    distance     = bus.busToStationNiheDistance + Σ dislist[bus.index .. target_order-2].d
    eta_minutes  = 行驶中: 车到下一站的距离/上一段速度
                   + Σ 每段(段长/段速度) + 0.5 分钟/站（停靠耗时）

其中 speedlist[].speed 的单位是 **米/分钟**。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("astrbot")

BASE_URL = "https://h5.mygolbs.com"
API_URL = BASE_URL + "/ApiData.do"
WARMUP_URL = BASE_URL + "/"

# 默认城市（插件配置里可改，用户也能用 /buscity 切换）
DEFAULT_CITY = "三明市"

# 城市列表缓存有效期（秒），一个月刷新一次就够了
_CITY_CACHE_SECONDS = 30 * 24 * 3600

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

# 默认每段速度的兜底值（米/分钟），约 24km/h
_FALLBACK_SPEED = 400.0
# 每停靠一站平均耗时（分钟）
_DWELL_MINUTES = 0.5


class BusApiError(Exception):
    """接口调用异常。"""


@dataclass
class Arrival:
    """一辆车到候车站的估算结果。

    stops_away 为 0 时表示车辆当前就停靠在候车站。
    """

    plate: str
    stops_away: int
    eta_minutes: float
    distance_m: float
    current_station: str
    stopped: bool

    @property
    def stops_text(self) -> str:
        if self.stops_away <= 0:
            return "已到站"
        if self.stops_away == 1:
            return "即将到站"
        return f"还有 {self.stops_away} 站"

    @property
    def eta_text(self) -> str:
        if self.stops_away <= 0:
            return "已到站"
        if self.distance_m < 300 or self.eta_minutes < 1:
            return "小于1分钟"
        if self.eta_minutes < 60:
            return f"{round(self.eta_minutes)}分钟"
        hours = int(self.eta_minutes // 60)
        minutes = round(self.eta_minutes % 60)
        return f"{hours}小时{minutes}分钟"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_polyline(raw: Any) -> list[tuple[float, float]]:
    """把 CMD=103 的 nihelist 解析成 [(lng, lat), ...]。"""
    points: list[tuple[float, float]] = []
    for item in raw or []:
        lng = _to_float((item or {}).get("lng"))
        lat = _to_float((item or {}).get("lat"))
        if lng and lat:
            points.append((lng, lat))
    return points


def _seg_speed(speedlist: list, index: int) -> float:
    """取第 index 段的速度（米/分钟），越界或用 0 时返回兜底值。"""
    if 0 <= index < len(speedlist):
        speed = _to_float((speedlist[index] or {}).get("speed"))
        if speed > 0:
            return speed
    return _FALLBACK_SPEED


def estimate(
    bus: dict, target_order: int, dislist: list, speedlist: list
) -> tuple[int, float, float] | None:
    """估算一辆车到 target_order 站的剩余站数 / 距离(米) / 时间(分钟)。

    target_order 为 1-based 站序；已经开过候车站的车辆返回 None。
    """
    index = int(_to_float(bus.get("index")))
    order = index + 1
    at_station = str(bus.get("statusType", "")) == "0"

    if order > target_order:
        return None

    stops_away = target_order - order
    if not at_station:
        # 车辆尚在途中，需要先开到它当前指向的那一站
        stops_away += 1

    distance = _to_float(bus.get("busToStationNiheDistance"))
    eta = 0.0
    if not at_station:
        eta += distance / _seg_speed(speedlist, index - 1)

    # 目标站 0-based 索引为 target_order-1，最后一段的索引是 target_order-2
    end = target_order - 2
    for i in range(index, end + 1):
        if i >= len(dislist):
            break
        seg = _to_float((dislist[i] or {}).get("d"))
        speed = _seg_speed(speedlist, i)
        distance += seg
        if at_station and i == index:
            eta += seg / speed
        else:
            eta += seg / speed + _DWELL_MINUTES

    return stops_away, distance, eta


def _fmt_hhmm(epoch_ms: float) -> str:
    if not epoch_ms:
        return ""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch_ms / 1000).strftime("%H:%M")


class BusClient:
    """掌上公交接口的异步客户端（整个插件共用一个实例）。"""

    def __init__(
        self,
        default_city: str = DEFAULT_CITY,
        timeout: float = 10.0,
    ) -> None:
        self.default_city = default_city
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": _UA,
                "Referer": WARMUP_URL,
                "Origin": BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        self._warmed = False
        self._lock = asyncio.Lock()
        self._cities: list[str] = []
        self._cities_at = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _warmup(self) -> None:
        """GET 首页以获取会话。"""
        async with self._lock:
            if self._warmed:
                return
            try:
                await self._client.get(WARMUP_URL)
            except httpx.HTTPError as exc:
                raise BusApiError(f"会话预热失败：{exc}") from exc
            self._warmed = True

    async def _post(
        self,
        payload: dict,
        city: str | None = None,
        retry: bool = True,
        expect_ok: bool = True,
    ) -> dict:
        """city=None 用默认城市；city="" 表示不传 CITYNAME（城市列表 / 城市校验用）。

        expect_ok=False 时不把 status != 1 当失败，交给调用方判断。
        """
        await self._warmup()
        data = dict(payload)
        resolved = self.default_city if city is None else city
        if resolved:
            data["CITYNAME"] = resolved
        try:
            resp = await self._client.post(API_URL, data=data)
            resp.raise_for_status()
            result = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if retry:
                # 会话过期时服务端会返回密文（JSON 解析失败），重新预热后再试一次
                self._warmed = False
                logger.debug("mygolbs 请求失败，重新预热会话后重试：%s", exc)
                return await self._post(
                    payload, city=city, retry=False, expect_ok=expect_ok
                )
            raise BusApiError(f"请求公交接口失败：{exc}") from exc

        if not isinstance(result, dict):
            raise BusApiError("公交接口返回格式异常")
        if expect_ok and int(result.get("status", 0)) != 1:
            raise BusApiError(str(result.get("msg") or "公交接口返回失败"))
        return result

    # ---------------- 城市 ----------------

    async def list_cities(self, refresh: bool = False) -> list[str]:
        """全国可用城市名列表（CMD=101）。带内存缓存，默认一个月刷新。"""
        if (
            not refresh
            and self._cities
            and time.monotonic() - self._cities_at < _CITY_CACHE_SECONDS
        ):
            return self._cities

        result = await self._post({"CMD": "101"}, city="")
        cities = [
            str(item.get("cityname") or "").strip()
            for item in (result.get("data") or [])
            if item.get("cityname")
        ]
        if cities:
            self._cities = cities
            self._cities_at = time.monotonic()
        return self._cities

    async def city_exists(self, city_name: str) -> bool:
        """向 CMD=204 确认城市是否开通（未开通返回 status=-1）。网络异常会抛出 BusApiError。"""
        result = await self._post(
            {"CMD": "204", "CITYNAME": city_name}, city="", expect_ok=False
        )
        return int(_to_float(result.get("status"))) == 1

    # ---------------- 对外方法 ----------------

    async def search_lines(
        self, keyword: str = "", city: str | None = None
    ) -> list[dict]:
        """按关键字搜索线路，返回 [{line_name, direction, from, to, company}]。"""
        result = await self._post({"CMD": "119", "KEY": keyword}, city=city)
        # 注意：CMD=119 的 buslines 在顶层，不像其他 CMD 那样包在 data 里
        raw = result.get("buslines")
        if raw is None and isinstance(result.get("data"), dict):
            raw = result["data"].get("buslines")
        raw = raw or []
        lines: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for item in raw:
            name = (item.get("lineName") or "").strip()
            if not name:
                continue
            direction = int(_to_float(item.get("upperOrDown"), 1))
            if (name, direction) in seen:
                continue
            seen.add((name, direction))
            lines.append(
                {
                    "line_name": name,
                    "direction": direction,
                    "from": (item.get("from") or "").strip(),
                    "to": (item.get("to") or "").strip(),
                    "company": (item.get("company") or "").strip(),
                }
            )
        return lines

    async def get_route(
        self, line_name: str, direction: int, city: str | None = None
    ) -> dict:
        """获取线路详情（站点表 + 道路拟合点）。"""
        result = await self._post(
            {"CMD": "103", "LINENAME": line_name, "DIRECTION": str(direction)},
            city=city,
        )
        stations = [
            {
                "order": int(_to_float(st.get("stationOrder"))),
                "name": (st.get("stationName") or "").strip(),
                "lat": _to_float(st.get("station_lat")),
                "lng": _to_float(st.get("station_lon")),
                "running": st.get("stationsStatus") == 1,
            }
            for st in (result.get("data") or [])
        ]
        first_last = result.get("firstLast") or []
        return {
            "line_name": (result.get("routeName") or line_name).strip(),
            "direction": direction,
            "from_station": stations[0]["name"] if stations else "",
            "to_station": stations[-1]["name"] if stations else "",
            "first_time": (first_last[0].get("first") if first_last else "") or "",
            "last_time": (first_last[0].get("last") if first_last else "") or "",
            "stations": stations,
            # 道路拟合点（nihelist），画线路地图用；格式 [(lng, lat), ...]
            "polyline": _parse_polyline(result.get("nihelist")),
        }

    async def _fetch_realtime(
        self,
        line_name: str,
        direction: int,
        station_order: int,
        city: str | None = None,
    ) -> dict:
        return await self._post(
            {
                "CMD": "104",
                "LINENAME": line_name,
                "DIRECTION": str(direction),
                "STATIONORDER": str(station_order),
            },
            city=city,
        )

    async def query(
        self,
        line_name: str,
        direction: int,
        station_order: int,
        top_n: int = 3,
        city: str | None = None,
    ) -> dict:
        """查询候车站最近 top_n 辆车的到站情况。

        返回 {ok, reason, arrivals, buses, plan_time, data_time}
        buses 是全部在途车辆的坐标与朝向（含已过候车站的车），画线路地图用。
        """
        result = await self._fetch_realtime(line_name, direction, station_order, city)
        out: dict[str, Any] = {
            "ok": True,
            "reason": "",
            "arrivals": [],
            "buses": [],
            "plan_time": (result.get("planTime") or "").strip(),
            "data_time": "",
        }

        if int(_to_float(result.get("runState"))) == 1:
            out["ok"] = False
            out["reason"] = "该线路当前已停运"
            return out
        if int(_to_float(result.get("hasReal"), 1)) == 0:
            out["ok"] = False
            out["reason"] = "这条线路还没开通实时到站数据（部分城市整体未开通）"
            return out

        dislist = result.get("dislist") or []
        speedlist = result.get("speedlist") or []
        buses = result.get("list") or []

        arrivals: list[Arrival] = []
        for bus in buses:
            estimated = estimate(bus, station_order, dislist, speedlist)
            if estimated is None:
                continue
            stops_away, distance, eta = estimated
            arrivals.append(
                Arrival(
                    plate=str(bus.get("busNumber") or "未知车牌"),
                    stops_away=stops_away,
                    eta_minutes=eta,
                    distance_m=distance,
                    current_station=str(bus.get("stationName") or ""),
                    stopped=str(bus.get("statusType", "")) == "0",
                )
            )

        # 已停靠在候车站的排最前，其余按预计到达时间由近到远
        arrivals.sort(
            key=lambda a: (
                0 if a.stops_away <= 0 else 1,
                round(a.eta_minutes, 1),
                a.stops_away,
            )
        )
        out["arrivals"] = arrivals[: max(1, top_n)]

        # 画线路地图用：全部在途车辆的坐标与朝向（含已经开过候车站的车）
        buses_view: list[dict] = []
        for bus in buses:
            lat = _to_float(bus.get("bus_lat")) or _to_float(bus.get("station_lat"))
            lng = (
                _to_float(bus.get("bus_lng"))
                or _to_float(bus.get("station_lng"))
                or _to_float(bus.get("station_lon"))
            )
            if not lat or not lng:
                continue
            order = int(_to_float(bus.get("index"))) + 1
            buses_view.append(
                {
                    "plate": str(bus.get("busNumber") or ""),
                    "lat": lat,
                    "lng": lng,
                    "angle": _to_float(bus.get("angle")),
                    "order": order,
                    "passed": order > station_order,
                    "stopped": str(bus.get("statusType", "")) == "0",
                    "current_station": str(bus.get("stationName") or ""),
                }
            )
        out["buses"] = buses_view

        rec_times = [_to_float(b.get("_recTime")) for b in buses if b.get("_recTime")]
        if rec_times:
            out["data_time"] = _fmt_hhmm(max(rec_times))
        return out


def render_arrivals(
    line_name: str,
    direction_to: str,
    station_name: str,
    result: dict,
    city: str = "",
) -> str:
    """把查询结果渲染成推送文案。"""
    title = f"🚌 {line_name}"
    if city:
        title = f"🚌 [{city}] {line_name}"
    if direction_to:
        title += f"（往 {direction_to}）"
    title += f" · 候车：{station_name}"

    lines = [title]
    if not result.get("ok"):
        lines.append(f"⚠️ {result.get('reason')}")
    else:
        arrivals: list[Arrival] = result.get("arrivals") or []
        if not arrivals:
            lines.append("暂无车辆 · 可能尚未发车")
        else:
            for index, arrival in enumerate(arrivals, 1):
                if arrival.stops_away <= 0:
                    lines.append(f"{index}. {arrival.plate} · 已到站")
                else:
                    lines.append(
                        f"{index}. {arrival.plate} · {arrival.stops_text} · 约 {arrival.eta_text}"
                    )

    if result.get("plan_time"):
        lines.append(f"🕘 起点预计发车：{result['plan_time']}")
    if result.get("data_time"):
        lines.append(f"⏱ 数据时间 {result['data_time']}")
    return "\n".join(lines)
