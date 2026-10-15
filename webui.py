"""插件 WebUI（Dashboard 插件页面）的后端接口。

AstrBot 会扫描插件目录下 `pages/<页面名>/index.html` 暴露 Dashboard 页面，
页面里的 `window.AstrBotPluginPage` 会把请求转发到
`/api/plug/<插件名>/<endpoint>`，所以这里注册的路由都必须带上插件名前缀。

页面与指令共享同一套订阅数据，改动立即生效，无需重启或重载插件。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from astrbot.api.event import MessageChain
from quart import jsonify, request

from .alarm_store import (
    RULE_EVERYDAY,
    RULE_RESTDAY,
    RULE_WEEKDAYS,
    RULE_WORKDAY,
    RepeatRuleError,
    describe_repeat,
    is_due_today,
    parse_repeat,
)
from .bus_api import Arrival, BusApiError, render_arrivals

logger = logging.getLogger("astrbot")

DIRECTION_LABEL = {1: "上行", 2: "下行"}

_REPEAT_SHORTCUTS = {
    RULE_EVERYDAY: "每天",
    RULE_WORKDAY: "工作日",
    RULE_RESTDAY: "非工作日",
}


def _ok(data: Any):
    return jsonify({"ok": True, "data": data})


def _fail(message: str):
    return jsonify({"ok": False, "error": message})


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是", "启用")


class BusAlarmWebUI:
    """把插件的订阅管理、线路查询、节假日等能力暴露成 WebUI 接口。"""

    def __init__(self, plugin, plugin_name: str) -> None:
        self.plugin = plugin
        self.plugin_name = plugin_name
        self.base = f"/{plugin_name}"

    # ---------------- 注册 ----------------

    def register(self) -> list[str]:
        """注册全部接口，返回路由列表（用于日志）。"""
        routes: list[tuple[str, Any, list[str], str]] = [
            ("overview", self.overview, ["GET"], "插件状态、订阅列表与节假日摘要"),
            ("cities", self.cities, ["GET"], "搜索掌上公交支持的城市"),
            ("lines", self.lines, ["GET"], "按关键词搜索线路"),
            ("route", self.route, ["GET"], "线路站点表"),
            ("realtime", self.realtime, ["GET"], "查询实时到站"),
            ("map", self.map_view, ["GET"], "线路地图（含实时车辆位置）"),
            ("alarm/create", self.alarm_create, ["POST"], "新增订阅"),
            ("alarm/update", self.alarm_update, ["POST"], "修改订阅"),
            ("alarm/toggle", self.alarm_toggle, ["POST"], "启用或暂停订阅"),
            ("alarm/delete", self.alarm_delete, ["POST"], "删除订阅"),
            ("alarm/push", self.alarm_push, ["POST"], "立即查询并推送到目标会话"),
            ("calendar/refresh", self.calendar_refresh, ["POST"], "重新拉取节假日数据"),
        ]
        for path, handler, methods, desc in routes:
            self.plugin.context.register_web_api(
                f"{self.base}/{path}", handler, methods, desc
            )
        return [path for path, *_ in routes]

    # ---------------- 入参 ----------------

    async def _payload(self) -> dict:
        """合并 URL 参数与 JSON 请求体，两种传参方式都能用。"""
        data: dict[str, Any] = {}
        args = getattr(request, "args", None)
        if args:
            data.update({str(key): args[key] for key in args})
        try:
            body = await request.get_json(silent=True)
        except RuntimeError:
            body = None
        if isinstance(body, dict):
            data.update({str(key): value for key, value in body.items()})
        return data

    async def _city_of(self, value: Any) -> str:
        """校验城市名，允许只写「福州」并自动补「市」。"""
        token = str(value or "").strip()
        if not token:
            return str(self.plugin.client.default_city)
        candidates = [token]
        if not token.endswith("市"):
            candidates.append(token + "市")
        for candidate in candidates:
            if await self.plugin.client.city_exists(candidate):
                return candidate
        raise ValueError(f"掌上公交暂不支持「{token}」")

    # ---------------- 读接口 ----------------

    async def overview(self):
        """插件状态、全部订阅与节假日摘要。"""
        plugin = self.plugin
        await plugin.refresh_calendar_if_needed()

        alarms = [self._alarm_view(item) for item in plugin.store.all()]
        alarms.sort(key=lambda item: (item["time"], _as_int(item["id"])))
        return _ok(
            {
                "pluginName": self.plugin_name,
                "defaultCity": plugin.client.default_city,
                "sessions": sorted({item["umo"] for item in alarms if item["umo"]}),
                "alarms": alarms,
                "stats": {
                    "total": len(alarms),
                    "enabled": sum(1 for item in alarms if item["enabled"]),
                    "cities": sorted({item["city"] for item in alarms}),
                },
                "calendar": self._calendar_view(),
            }
        )

    async def cities(self):
        """搜索支持的城市。"""
        payload = await self._payload()
        keyword = str(payload.get("q") or "").strip()
        try:
            found = await self.plugin.client.list_cities()
        except BusApiError as exc:
            return _fail(f"获取城市列表失败：{exc}")
        if keyword:
            found = [name for name in found if keyword in name]
        return _ok({"total": len(found), "cities": found[:80]})

    async def lines(self):
        """按关键词搜索线路。"""
        payload = await self._payload()
        keyword = str(payload.get("q") or "").strip()
        if not keyword:
            return _fail("请输入线路关键词，例如 1路")
        try:
            city = await self._city_of(payload.get("city"))
            found = await self.plugin.client.search_lines(keyword, city=city)
        except (ValueError, BusApiError) as exc:
            return _fail(str(exc))
        return _ok(
            {
                "city": city,
                "keyword": keyword,
                "total": len(found),
                "lines": found[:40],
            }
        )

    async def route(self):
        """线路站点表。"""
        payload = await self._payload()
        line_name = str(payload.get("line_name") or "").strip()
        if not line_name:
            return _fail("请先选择线路")
        direction = _as_int(payload.get("direction"), 1)
        try:
            city = await self._city_of(payload.get("city"))
            route = await self.plugin.client.get_route(line_name, direction, city=city)
        except (ValueError, BusApiError) as exc:
            return _fail(str(exc))
        if not route["stations"]:
            return _fail(f"{city} 的 {line_name} 方向{direction} 没有站点数据")
        return _ok(
            {
                "city": city,
                "directionLabel": DIRECTION_LABEL.get(direction, ""),
                **route,
            }
        )

    async def realtime(self):
        """查询候车站最近几辆车的到站情况。

        `station_order` 与 `station` 二者其一即可：只给站名时会先解析站序。
        """
        try:
            target = await self._query_target(await self._payload())
        except (ValueError, BusApiError) as exc:
            return _fail(str(exc))
        result = target["result"]
        line_name = target["lineName"]
        city = target["city"]
        return _ok(
            {
                "city": city,
                "ok": bool(result.get("ok")),
                "reason": result.get("reason") or "",
                "planTime": result.get("plan_time") or "",
                "dataTime": result.get("data_time") or "",
                "text": render_arrivals(
                    line_name,
                    target["directionTo"],
                    target["stationName"],
                    result,
                    city=city,
                ),
                "arrivals": [
                    self._arrival_view(item) for item in (result.get("arrivals") or [])
                ],
            }
        )

    async def map_view(self):
        """渲染「线路走向 + 实时车辆位置」地图，返回 data URI。"""
        try:
            target = await self._query_target(await self._payload())
        except (ValueError, BusApiError) as exc:
            return _fail(str(exc))

        image_b64 = await self.plugin.build_map_image(
            target["lineName"],
            target["direction"],
            target["stationOrder"],
            target["stationName"],
            target["directionTo"],
            target["city"],
            target["result"],
            route=target["route"],
        )
        if not image_b64:
            return _fail("地图渲染失败，这条线路可能没有可用的经纬度数据")

        result = target["result"]
        return _ok(
            {
                "city": target["city"],
                "lineName": target["lineName"],
                "directionTo": target["directionTo"],
                "stationName": target["stationName"],
                "stationOrder": target["stationOrder"],
                "planTime": result.get("plan_time") or "",
                "dataTime": result.get("data_time") or "",
                "busCount": len(result.get("buses") or []),
                "arrivals": [
                    self._arrival_view(item) for item in (result.get("arrivals") or [])
                ],
                "image": f"data:image/png;base64,{image_b64}",
            }
        )

    async def _query_target(self, payload: dict) -> dict:
        """把前端的线路 / 站点参数解析成一次实时查询。

        `station_order` 与 `station` 二者其一即可：只给站名时会先解析站序。
        """
        line_name = str(payload.get("line_name") or "").strip()
        if not line_name:
            raise ValueError("请先选择线路")
        direction = _as_int(payload.get("direction"), 1)
        top_n = max(1, _as_int(payload.get("top_n"), self.plugin.default_top_n))
        station_order = _as_int(payload.get("station_order"))
        station_name = str(payload.get("station_name") or "").strip()
        direction_to = str(payload.get("direction_to") or "").strip()

        city = await self._city_of(payload.get("city"))
        if station_order <= 0:
            token = str(payload.get("station") or station_name).strip()
            if not token:
                raise ValueError("缺少候车站信息")
            resolved = await self.plugin.resolve_station(
                line_name, direction, token, city
            )
            station_order = resolved["station_order"]
            station_name = resolved["station_name"]
            direction_to = direction_to or resolved["route"]["to_station"]
            route = resolved["route"]
        else:
            route = await self.plugin.client.get_route(line_name, direction, city=city)
        result = await self.plugin.client.query(
            line_name, direction, station_order, top_n, city=city
        )
        return {
            "city": city,
            "lineName": line_name,
            "direction": direction,
            "directionTo": direction_to,
            "stationName": station_name,
            "stationOrder": station_order,
            "topN": top_n,
            "route": route,
            "result": result,
        }

    # ---------------- 写接口 ----------------

    async def alarm_create(self):
        """新增一条订阅。"""
        payload = await self._payload()
        try:
            umo = str(payload.get("umo") or "").strip()
            if not umo:
                raise ValueError("请填写目标会话（形如 aiocqhttp:GroupMessage:123456）")
            fire_time = self.plugin.parse_time(str(payload.get("time") or ""))
            repeat = parse_repeat(str(payload.get("repeat") or "每天"))
            city = await self._city_of(payload.get("city"))
            direction = _as_int(payload.get("direction"), 0)
            if direction not in (1, 2):
                raise ValueError("方向只能是 1（上行）或 2（下行）")
            line_name = str(payload.get("line_name") or "").strip()
            if not line_name:
                raise ValueError("请填写线路名")
            resolved = await self.plugin.resolve_station(
                line_name, direction, str(payload.get("station") or ""), city
            )
        except (ValueError, RepeatRuleError, BusApiError) as exc:
            return _fail(str(exc))

        route = resolved["route"]
        item = self.plugin.store.add(
            {
                "umo": umo,
                "time": fire_time,
                "repeat": repeat,
                "city": city,
                "line_name": route["line_name"],
                "direction": direction,
                "direction_to": route["to_station"],
                "station_order": resolved["station_order"],
                "station_name": resolved["station_name"],
                "top_n": max(
                    1, _as_int(payload.get("top_n"), self.plugin.default_top_n)
                ),
                "kind": "subscription",
            }
        )
        logger.info(
            "WebUI 新增订阅 [%s] %s %s", item["id"], fire_time, route["line_name"]
        )
        return _ok({"alarm": self._alarm_view(item)})

    async def alarm_update(self):
        """修改订阅；改动线路 / 站点时会重新解析站序。"""
        payload = await self._payload()
        store = self.plugin.store
        item = store.get(str(payload.get("id") or ""))
        if item is None:
            return _fail("没有找到这条订阅")

        try:
            if "enabled" in payload:
                item["enabled"] = _as_bool(payload.get("enabled"))
            if payload.get("time"):
                item["time"] = self.plugin.parse_time(str(payload["time"]))
            if payload.get("repeat"):
                item["repeat"] = parse_repeat(str(payload["repeat"]))
            if payload.get("top_n"):
                item["top_n"] = max(1, _as_int(payload.get("top_n"), 1))
            if (
                payload.get("city")
                or payload.get("line_name")
                or payload.get("station")
            ):
                city = await self._city_of(payload.get("city") or item.get("city"))
                line_name = str(
                    payload.get("line_name") or item.get("line_name") or ""
                ).strip()
                direction = _as_int(
                    payload.get("direction"), _as_int(item.get("direction"), 1)
                )
                station = str(payload.get("station") or item.get("station_name") or "")
                resolved = await self.plugin.resolve_station(
                    line_name, direction, station, city
                )
                route = resolved["route"]
                item.update(
                    {
                        "city": city,
                        "line_name": route["line_name"],
                        "direction": direction,
                        "direction_to": route["to_station"],
                        "station_order": resolved["station_order"],
                        "station_name": resolved["station_name"],
                    }
                )
        except (ValueError, RepeatRuleError, BusApiError) as exc:
            return _fail(str(exc))

        store.save()
        return _ok({"alarm": self._alarm_view(item)})

    async def alarm_toggle(self):
        """启用或暂停订阅；不传 enabled 时按当前状态取反。"""
        payload = await self._payload()
        item = self.plugin.store.get(str(payload.get("id") or ""))
        if item is None:
            return _fail("没有找到这条订阅")
        value = payload.get("enabled")
        item["enabled"] = (
            (not item.get("enabled", True)) if value is None else _as_bool(value)
        )
        self.plugin.store.save()
        return _ok({"alarm": self._alarm_view(item)})

    async def alarm_delete(self):
        """删除订阅。"""
        payload = await self._payload()
        sub_id = str(payload.get("id") or "")
        if not self.plugin.store.remove(sub_id):
            return _fail("没有找到这条订阅")
        return _ok({"id": sub_id})

    async def alarm_push(self):
        """立即查询一次并推送到该订阅对应的会话（不会占用当天的定时触发）。"""
        payload = await self._payload()
        item = self.plugin.store.get(str(payload.get("id") or ""))
        if item is None:
            return _fail("没有找到这条订阅")

        city = str(item.get("city") or self.plugin.client.default_city)
        top_n = max(1, _as_int(item.get("top_n"), self.plugin.default_top_n))
        try:
            result = await self.plugin.client.query(
                item["line_name"],
                _as_int(item.get("direction"), 1),
                _as_int(item.get("station_order")),
                top_n,
                city=city,
            )
        except BusApiError as exc:
            return _fail(f"查询失败：{exc}")

        text = render_arrivals(
            item["line_name"],
            item.get("direction_to") or "",
            item.get("station_name") or "",
            result,
            city=city,
        )

        umo = str(item.get("umo") or "")
        chain = MessageChain().message(text)
        chain = await self.plugin.attach_map(
            chain,
            line_name=item["line_name"],
            direction=_as_int(item.get("direction"), 1),
            station_order=_as_int(item.get("station_order")),
            station_name=item.get("station_name") or "",
            direction_to=item.get("direction_to") or "",
            city=city,
            result=result,
        )
        # attach_map 只在成功时追加一个图片组件，所以长度大于 1 即为带了地图
        has_map = len(chain.chain) > 1

        sent = False
        error = ""
        try:
            await self.plugin.context.send_message(umo, chain)
            sent = True
        except Exception as exc:
            logger.exception("WebUI 推送失败：%s", umo)
            error = str(exc)

        return _ok(
            {
                "umo": umo,
                "sent": sent,
                "error": error,
                "hasMap": has_map,
                "text": text,
                "arrivals": [
                    self._arrival_view(a) for a in (result.get("arrivals") or [])
                ],
            }
        )

    async def calendar_refresh(self):
        """强制重新拉取在线节假日数据。"""
        calendar = self.plugin.calendar
        if calendar is None:
            return _fail("在线节假日数据已在插件配置里关闭（holiday_api）")
        try:
            await calendar.ensure_fresh(force=True)
        except Exception as exc:
            logger.exception("WebUI 刷新节假日失败")
            return _fail(f"拉取失败：{exc}")
        self.plugin.sync_calendar()
        return _ok({"calendar": self._calendar_view()})

    # ---------------- 视图转换 ----------------

    def _alarm_view(self, item: dict) -> dict:
        plugin = self.plugin
        repeat = item.get("repeat") or {}
        return {
            "id": str(item.get("id")),
            "enabled": bool(item.get("enabled", True)),
            "time": str(item.get("time") or ""),
            "repeatRaw": self._repeat_raw(repeat),
            "repeatText": describe_repeat(repeat),
            "dueToday": is_due_today(
                repeat, dt.date.today(), plugin.holidays, plugin.workdays
            ),
            "city": str(item.get("city") or plugin.client.default_city),
            "lineName": str(item.get("line_name") or ""),
            "direction": _as_int(item.get("direction"), 1),
            "directionLabel": DIRECTION_LABEL.get(
                _as_int(item.get("direction"), 1), ""
            ),
            "directionTo": str(item.get("direction_to") or ""),
            "stationName": str(item.get("station_name") or ""),
            "stationOrder": _as_int(item.get("station_order")),
            "topN": _as_int(item.get("top_n"), plugin.default_top_n),
            "umo": str(item.get("umo") or ""),
            "lastFireDate": str(item.get("last_fire_date") or ""),
        }

    def _calendar_view(self) -> dict:
        plugin = self.plugin
        today = dt.date.today()
        today_iso = today.isoformat()
        if today_iso in plugin.holidays:
            today_state = "休息日（法定节假日）"
        elif today_iso in plugin.workdays:
            today_state = "工作日（调休补班）"
        elif today.isoweekday() >= 6:
            today_state = "休息日（周末）"
        else:
            today_state = "工作日（周一至周五）"

        manual_holidays = getattr(plugin, "_manual_holidays", set())
        manual_workdays = getattr(plugin, "_manual_workdays", set())
        return {
            "enabled": plugin.calendar is not None,
            "describe": plugin.calendar.describe() if plugin.calendar else "",
            "today": today_iso,
            "todayState": today_state,
            "upcomingHolidays": sorted(d for d in plugin.holidays if d >= today_iso)[
                :10
            ],
            "upcomingWorkdays": sorted(d for d in plugin.workdays if d >= today_iso)[
                :10
            ],
            "holidays": sorted(plugin.holidays),
            "workdays": sorted(plugin.workdays),
            "manualHolidays": sorted(manual_holidays),
            "manualWorkdays": sorted(manual_workdays),
        }

    @staticmethod
    def _repeat_raw(rule: dict) -> str:
        """把结构化规则还原成可再次提交的文本。"""
        rule_type = (rule or {}).get("type")
        if rule_type == RULE_WEEKDAYS:
            return "".join(str(day) for day in sorted(rule.get("days") or []))
        return _REPEAT_SHORTCUTS.get(str(rule_type), "每天")

    @staticmethod
    def _arrival_view(arrival: Arrival) -> dict:
        return {
            "plate": arrival.plate,
            "stopsText": arrival.stops_text,
            "etaText": arrival.eta_text,
            "stopsAway": arrival.stops_away,
            "etaMinutes": round(arrival.eta_minutes, 1),
            "distanceM": round(arrival.distance_m),
            "currentStation": arrival.current_station,
            "stopped": arrival.stopped,
        }
