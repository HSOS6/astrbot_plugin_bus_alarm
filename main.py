"""公交赶车闹钟（全国版）。

到达设定时间后，主动推送候车站最近数辆公交车的到站信息。
数据源为「掌上公交」H5 接口（h5.mygolbs.com），按城市名查询，覆盖全国 500+ 城市。

指令：
    /buscity [城市名]                 查看 / 切换所在城市
    /buscity list [关键词]            查看支持的城市
    /buslines <线路名> [站点关键词]   查看线路站点与站序
    /busadd <HH:MM> <重复> <线路名> <方向> <站序或站名> [车辆数]
                                      添加订阅
    /buslist                          查看我的订阅
    /busdel <编号>                    删除订阅
    /buson  <编号>                    启用订阅
    /busoff <编号>                    暂停订阅
    /busnow <编号>                    立即查询一次
    /busstop                          测试主动推送是否可用
    /buscalendar                      查看节假日 / 补班日数据
    /bushelp                          查看帮助

Dashboard 插件页面（AstrBot WebUI → 插件 → 本插件卡片）：
    pages/bus-alarm/ 提供图形化的订阅管理、线路查询与节假日查看，
    后端接口在 webui.py 中注册，改动与指令共享同一份订阅数据。

线路地图：
    配置项 map_in_push 打开后，推送会在文字下方附一张「线路走向 + 车辆位置」
    的图片（底图来自高德公开瓦片，无需 key），渲染逻辑见 map_render.py。
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .alarm_store import (
    AlarmStore,
    CityStore,
    RepeatRuleError,
    _split_dates,
    can_manage,
    describe_repeat,
    parse_repeat,
)
from .bus_api import DEFAULT_CITY, BusApiError, BusClient, render_arrivals
from .holiday_api import HolidayCalendar
from .profile import (
    ProfileStore,
    bucket_label,
    bucket_of,
    day_kind_of,
    in_window,
    parse_window,
)
from .scheduler import AlarmScheduler
from .webui import BusAlarmWebUI

PLUGIN_NAME = "astrbot_plugin_bus_alarm"
DEFAULT_TOP_N = 3

HELP_TEXT = (
    "🚌 公交赶车闹钟\n"
    "——————————————\n"
    "⓪ 先确定城市（默认 {city}）\n"
    "   /buscity 福州      切换城市\n"
    "   /buscity list 泉州 搜索支持的城市\n"
    "① 再查站序\n"
    "   /buslines 1路 附小\n"
    "② 最后加订阅\n"
    "   /busadd 07:30 工作日 1路 1 附小 3\n"
    "   ⤷ 时间 HH:MM · 重复规则 · 线路 · 方向 · 站序或站名 · 显示车辆数\n"
    "   ⤷ 重复规则可填：每天 / 工作日 / 非工作日 / 12345(周几，1=周一)\n"
    "——————————————\n"
    "💬 也可以直接说，不用记指令：\n"
    "   「我在市政府，1路到哪了」\n"
    "   ⤷ 没说方向时，会按你的通勤时段与出行习惯自动判断\n"
    "   「每天早上7点半提醒我1路」→ 创建订阅\n"
    "   「把2号改成8点」/「暂停1号」/「删掉3号」→ 改 / 停 / 删\n"
    "   ⤷ 订阅归创建者，别人只能管自己建的，管理员不受限制\n"
    "——————————————\n"
    "/buslist         查看我的订阅\n"
    "/busdel  <编号>  删除订阅\n"
    "/buson   <编号>  启用订阅\n"
    "/busoff  <编号>  暂停订阅\n"
    "/busnow  <编号>  立即查询一次\n"
    "/buscalendar     查看节假日 / 补班日数据\n"
    "——————————————\n"
    "/bushabit        查看学到的出行习惯\n"
    "/busplace        设置家 / 公司的站点（帮助推断方向）\n"
    "——————————————\n"
    "也可以在 AstrBot WebUI 的插件页面里图形化管理订阅"
)


class BusAlarm(Star):
    """公交赶车闹钟插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        self.client = BusClient(
            default_city=self._cfg("default_city", DEFAULT_CITY) or DEFAULT_CITY,
            timeout=float(self._cfg("request_timeout", 10) or 10),
        )

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = AlarmStore(data_dir)
        self.cities = CityStore(data_dir)
        self.scheduler = AlarmScheduler(self)

        # 在线节假日数据（失败时自动退化为「只按周末判断」）
        self.calendar = HolidayCalendar(
            data_dir,
            timeout=float(self._cfg("request_timeout", 10) or 10),
            cache_days=int(self._cfg("holiday_cache_days", 7) or 7),
        )
        if not self._cfg("holiday_api", True):
            self.calendar = None

        # 配置里手填的日期，作为在线数据的补充
        self._manual_holidays = _split_dates(self._cfg("holidays", ""))
        self._manual_workdays = _split_dates(self._cfg("workdays", ""))
        self.holidays: set[str] = set(self._manual_holidays)
        self.workdays: set[str] = set(self._manual_workdays)

        self.default_top_n = int(
            self._cfg("default_top_n", DEFAULT_TOP_N) or DEFAULT_TOP_N
        )
        # 推送时是否附带「线路走向 + 车辆位置」地图
        self.map_in_push = bool(self._cfg("map_in_push", True))

        # 自然语言查询与出行习惯学习
        self.nl_query = bool(self._cfg("nl_query", True))
        # 自然语言「创建 / 修改 / 删除订阅」（与查询分开的开关）
        self.nl_subscribe = bool(self._cfg("nl_subscribe", True))
        self.habit_enabled = bool(self._cfg("habit_enabled", True))
        self.habit_min_samples = max(1, int(self._cfg("habit_min_samples", 2) or 2))
        self.place_auto_learn = bool(self._cfg("place_auto_learn", True))
        self.commute_am = parse_window(
            self._cfg("commute_am", "07:00-09:00"), (7 * 60, 9 * 60)
        )
        self.commute_pm = parse_window(
            self._cfg("commute_pm", "17:00-19:00"), (17 * 60, 19 * 60)
        )
        self.profile = ProfileStore(
            data_dir,
            retention_days=int(self._cfg("habit_retention_days", 30) or 30),
        )

        # Dashboard 插件页面的后端接口（页面在 pages/bus-alarm/ 下）
        self.webui = BusAlarmWebUI(self, PLUGIN_NAME)
        self.webui_paths = self.webui.register()

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value in (None, "") else value

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        if self.calendar is not None:
            self.calendar.load_cache()
            self.sync_calendar()
            # 拉取在线数据可能耗时，放到后台，不阻塞插件加载
            self._calendar_task = asyncio.create_task(
                self._refresh_calendar(), name="bus_alarm_calendar"
            )
        self.scheduler.start()
        logger.info(
            "公交赶车闹钟已启动，默认城市 %s，当前 %d 条订阅，数据目录：%s\n"
            "节假日数据：%s\n"
            "WebUI 页面：pages/bus-alarm（%d 个接口）",
            self.client.default_city,
            len(self.store.all()),
            self.store.path.parent,
            self.calendar.describe()
            if self.calendar
            else "已关闭在线数据，仅按周末判断",
            len(self.webui_paths),
        )

    async def terminate(self) -> None:
        await self.scheduler.stop()
        task = getattr(self, "_calendar_task", None)
        if task is not None and not task.done():
            task.cancel()
        if self.calendar is not None:
            await self.calendar.close()
        await self.client.close()

    # ---------------- 节假日日历 ----------------

    def sync_calendar(self) -> None:
        """把在线数据与手填配置合并成最终判定集合。"""
        online_holidays = self.calendar.holidays if self.calendar else set()
        online_workdays = self.calendar.workdays if self.calendar else set()
        self.holidays = set(online_holidays) | self._manual_holidays
        self.workdays = set(online_workdays) | self._manual_workdays

    async def refresh_calendar_if_needed(self) -> None:
        """缓存过期时重新拉取，并刷新判定集合。"""
        if self.calendar is None:
            return
        try:
            if await self.calendar.ensure_fresh():
                self.sync_calendar()
        except Exception:
            logger.exception("刷新节假日数据失败")

    async def _refresh_calendar(self) -> None:
        await self.refresh_calendar_if_needed()

    # ---------------- 工具方法 ----------------

    def _city_for(self, event: AstrMessageEvent) -> str:
        """当前会话使用的城市（用户用 /buscity 设过就用它，否则用插件默认）。"""
        return self.cities.get(event.unified_msg_origin, self.client.default_city)

    # ---------------- 订阅归属与权限 ----------------

    @staticmethod
    def _sender_identity(event: AstrMessageEvent) -> tuple[str, str]:
        """取操作者的发送者 ID 与昵称，取不到就返回空串（老版本接口缺失时兜底）。"""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        try:
            sender_name = str(event.get_sender_name() or "")
        except Exception:
            sender_name = ""
        return sender_id, sender_name

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """判断操作者是否为 AstrBot 管理员（配置里手动添加的 ID）。"""
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _can_manage(self, event: AstrMessageEvent, item: dict | None) -> bool:
        """这条订阅是否归当前操作者管（管理员无限制）。"""
        sender_id, _ = self._sender_identity(event)
        return can_manage(
            item,
            sender_id,
            is_admin=self._is_admin(event),
            umo=event.unified_msg_origin,
        )

    def _own_items(self, event: AstrMessageEvent, umo: str) -> list[dict]:
        """当前操作者在本会话能管理的订阅（管理员返回该会话全部订阅）。"""
        items = self.store.by_umo(umo)
        if self._is_admin(event):
            return items
        sender_id, _ = self._sender_identity(event)
        return [
            item
            for item in items
            if can_manage(item, sender_id, umo=event.unified_msg_origin)
        ]

    @staticmethod
    def _denied_text(sub_id: str, item: dict | None) -> str:
        """越权操作时的提示。"""
        owner = str(
            (item or {}).get("owner_name") or (item or {}).get("owner_id") or ""
        )
        who = f"由 {owner} 创建" if owner else "不是你创建的"
        return f"🚫 订阅 [{sub_id}] {who}，只有创建者或管理员可以操作"

    @staticmethod
    def parse_time(text: str) -> str:
        """把用户输入的时间规范成 HH:MM。"""
        raw = (text or "").strip().replace("：", ":")
        try:
            return dt.datetime.strptime(raw, "%H:%M").strftime("%H:%M")
        except ValueError:
            pass
        try:
            return dt.datetime.strptime(raw, "%H%M").strftime("%H:%M")
        except ValueError as exc:
            raise ValueError(f"时间格式不对：{raw}，请用 07:30 这样的格式") from exc

    async def resolve_station(
        self, line_name: str, direction: int, station: str, city: str
    ) -> dict:
        """把用户输入的站序或站名解析成站点信息。"""
        route = await self.client.get_route(line_name, direction, city=city)
        stations = route["stations"]
        if not stations:
            raise ValueError(f"没有找到 {line_name} 方向{direction} 的站点信息")

        token = (station or "").strip()
        if token.isdigit():
            order = int(token)
            hit = next((s for s in stations if s["order"] == order), None)
            if hit is None:
                raise ValueError(
                    f"{line_name} 方向{direction} 共 {len(stations)} 站，没有站序 {order}"
                )
            return {"station_order": order, "station_name": hit["name"], "route": route}

        lowered = token.replace("（", "(").replace("）", ")")
        for st in stations:
            if st["name"] == token or st["name"] == lowered:
                return {
                    "station_order": st["order"],
                    "station_name": st["name"],
                    "route": route,
                }
        for st in stations:
            if token and token in st["name"]:
                return {
                    "station_order": st["order"],
                    "station_name": st["name"],
                    "route": route,
                }

        candidates = "、".join(s["name"] for s in stations[:6])
        raise ValueError(
            f"在 {line_name} 方向{direction} 里没找到「{token}」。前几站：{candidates}"
        )

    # ---------------- 自然语言查询：方向推断 ----------------

    @staticmethod
    def _same_station(left: str, right: str) -> bool:
        """站名比较（含前后缀差异，如「市政府」与「市政府站」）。"""
        a, b = (left or "").strip(), (right or "").strip()
        if not a or not b:
            return False
        return a == b or a in b or b in a

    @staticmethod
    def _match_order(stations: list[dict], name: str) -> int | None:
        """在线路站点表里定位一个站名（精确优先，其次包含）。"""
        token = (name or "").strip()
        if not token:
            return None
        for station in stations:
            if station["name"] == token:
                return int(station["order"])
        for station in stations:
            if token in station["name"] or station["name"] in token:
                return int(station["order"])
        return None

    @staticmethod
    def _direction_from_text(text: str, entries: list[dict]) -> int:
        """把 AI 提取的方向文本解析成 1 / 2；无法确定返回 0。"""
        token = (text or "").strip()
        if not token:
            return 0
        lowered = token.lower()
        if lowered in ("1", "上行", "去程", "去", "往程", "up", "outbound"):
            return 1
        if lowered in ("2", "下行", "回程", "回", "返程", "down", "inbound"):
            return 2
        # 用户说的是终点站名，比如「往火车站方向」
        for entry in entries or []:
            terminal = str(entry.get("to") or "")
            if terminal and (terminal in token or token in terminal):
                return int(entry["direction"])
        return 0

    async def _direction_between(
        self,
        line_name: str,
        city: str,
        origin: str,
        dest: str,
        available: list[int],
    ) -> int | None:
        """找出「先经过 origin 再经过 dest」的那个方向（跨线路通用）。"""
        for direction in available:
            try:
                route = await self.client.get_route(line_name, direction, city=city)
            except BusApiError:
                continue
            start = self._match_order(route["stations"], origin)
            end = self._match_order(route["stations"], dest)
            if start is not None and end is not None and start < end:
                return direction
        return None

    def _commute_intent(
        self, umo: str, station: str, now: dt.datetime
    ) -> tuple[str, str] | None:
        """判断用户此刻是否在「家 ↔ 公司」的通勤路上，返回（起点站, 终点站）。"""
        places = self.profile.places(umo)
        home = str((places.get("home") or {}).get("name") or "")
        work = str((places.get("work") or {}).get("name") or "")
        if not (home and work):
            return None

        minutes = now.hour * 60 + now.minute
        token = (station or "").strip()
        if token:
            if self._same_station(token, home) and in_window(minutes, self.commute_am):
                return home, work
            if self._same_station(token, work) and in_window(minutes, self.commute_pm):
                return work, home
            return None
        # 用户没说站点时，按时段猜：早上多半从家走，傍晚多半从公司走
        if in_window(minutes, self.commute_am):
            return home, work
        if in_window(minutes, self.commute_pm):
            return work, home
        return None

    async def _infer_direction(
        self,
        umo: str,
        line_name: str,
        city: str,
        station: str,
        available: list[int],
        now: dt.datetime,
    ) -> tuple[int, str] | None:
        """用户没说方向时的推断链：地点语义 → 时段习惯。"""
        # ① 地点语义 + 通勤时段
        intent = self._commute_intent(umo, station, now)
        if intent:
            origin, dest = intent
            found = await self._direction_between(
                line_name, city, origin, dest, available
            )
            if found in available:
                return found, f"通勤时段，按你平时从「{origin}」到「{dest}」的方向推的"

        # ② 同时段的出行习惯
        if self.habit_enabled:
            kind = day_kind_of(now, self.holidays, self.workdays)
            ticket = self.profile.predict(
                umo,
                now,
                kind,
                line=line_name,
                station=station,
                min_samples=self.habit_min_samples,
            )
            if ticket and int(ticket["direction"]) in available:
                return (
                    int(ticket["direction"]),
                    f"按你平时 {bucket_label(bucket_of(now))} 前后坐这趟车的习惯推的",
                )
        return None

    def _station_hint(
        self,
        umo: str,
        line_name: str,
        direction: int,
        station: str,
        now: dt.datetime,
        kind: str,
    ) -> str:
        """用户没说站名时，依次从通勤地点 / 出行习惯 / 已有订阅里找候车站。"""
        token = (station or "").strip()
        if token:
            return token

        intent = self._commute_intent(umo, "", now)
        if intent:
            return intent[0]

        ticket = self.profile.predict(umo, now, kind, line=line_name, min_samples=1)
        if ticket and ticket.get("station"):
            return str(ticket["station"])

        for item in self.store.by_umo(umo):
            if (
                item.get("line_name") == line_name
                and int(item.get("direction") or 0) == direction
            ):
                return str(item.get("station_name") or "")
        return ""

    @staticmethod
    def _best_eta(result: dict) -> float | None:
        """这一侧最近一辆车还有多久到（分钟）；没有车返回 None。"""
        best: float | None = None
        for arrival in result.get("arrivals") or []:
            value = 0.0 if arrival.stops_away <= 0 else arrival.eta_minutes
            best = value if best is None else min(best, value)
        return best

    async def _query_plan(
        self,
        umo: str,
        line_name: str,
        city: str,
        station: str,
        plan: list[tuple[int, str]],
        now: dt.datetime,
    ) -> tuple[list[dict], list[str]]:
        """按计划里的方向逐个查询，返回（成功项, 错误说明）。"""
        kind = day_kind_of(now, self.holidays, self.workdays)
        items: list[dict] = []
        errors: list[str] = []
        for direction, reason in plan:
            station_token = self._station_hint(
                umo, line_name, direction, station, now, kind
            )
            if not station_token:
                errors.append("不知道你要在哪个站候车")
                continue
            try:
                resolved = await self.resolve_station(
                    line_name, direction, station_token, city
                )
                result = await self.client.query(
                    line_name,
                    direction,
                    resolved["station_order"],
                    self.default_top_n,
                    city=city,
                )
            except BusApiError as exc:
                errors.append(f"方向{direction}查询失败：{exc}")
                continue
            except ValueError as exc:
                errors.append(str(exc))
                continue
            items.append(
                {
                    "direction": direction,
                    "reason": reason,
                    "resolved": resolved,
                    "result": result,
                }
            )
        return items, errors

    def _record_observation(
        self,
        umo: str,
        *,
        line: str,
        direction: int,
        station: str,
        city: str,
        source: str,
        now: dt.datetime,
    ) -> None:
        """把这次查询记进画像，并按需认家 / 认公司。"""
        if not self.habit_enabled:
            return
        self.profile.remember(
            umo,
            line=line,
            direction=direction,
            station=station,
            city=city,
            source=source,
            weight=1.0 if source == "explicit" else 0.3,
            when=now,
        )
        if self.place_auto_learn:
            self.profile.learn_places(umo, min_hits=self.habit_min_samples)

    async def _answer_arrival(
        self,
        event: AstrMessageEvent,
        line_name: str,
        station: str = "",
        direction: str = "",
        city: str = "",
    ):
        """自然语言查询主流程：认线路 → 定方向 → 解析候车站 → 输出到站信息。"""
        umo = event.unified_msg_origin
        line_token = (line_name or "").strip()
        if not line_token:
            yield event.plain_result(
                "要查哪条线路呢？比如「1路到哪了」，或用 /bushelp 看完整用法"
            )
            return

        city_now = (city or "").strip() or self._city_for(event)
        try:
            lines = await self.client.search_lines(line_token, city=city_now)
        except BusApiError as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return

        exact = [line for line in lines if line["line_name"] == line_token]
        if not exact and len(lines) == 1:
            exact = lines
            line_token = lines[0]["line_name"]
        if not exact:
            if not lines:
                yield event.plain_result(
                    f"在「{city_now}」没找到包含「{line_token}」的线路，"
                    "城市不对可以先用 /buscity 切换"
                )
                return
            body = "\n".join(
                f"· {line['line_name']}（方向{line['direction']}）"
                f"{line['from']} → {line['to']}"
                for line in lines[:10]
            )
            yield event.plain_result(
                f"「{line_token}」匹配到多条线路，用完整线路名再问一次：\n{body}"
            )
            return

        available = sorted({int(line["direction"]) for line in exact})
        now = dt.datetime.now()

        want = self._direction_from_text(direction, exact)
        if want in available:
            plan: list[tuple[int, str]] = [(want, "按你指定的方向查")]
            source = "explicit"
        else:
            inferred = await self._infer_direction(
                umo, line_token, city_now, station, available, now
            )
            if inferred:
                plan = [inferred]
                source = "inferred"
            else:
                plan = [
                    (item, "你没说方向，两个方向都查了，下面是有车的那边")
                    for item in available
                ]
                source = "inferred"

        items, errors = await self._query_plan(
            umo, line_token, city_now, station, plan, now
        )
        if not items:
            text = "这次没查到候车信息。"
            if errors:
                text += "\n" + "\n".join(dict.fromkeys(errors))
            text += "\n把站名一起告诉我就能查，例如「1路 市政府 到哪了」"
            yield event.plain_result(text)
            return

        chosen = items
        if len(items) > 1:
            scored = [(self._best_eta(item["result"]), item) for item in items]
            with_bus = sorted(
                (pair for pair in scored if pair[0] is not None),
                key=lambda pair: pair[0],
            )
            if len(with_bus) == 1 or (
                len(with_bus) >= 2 and with_bus[1][0] - with_bus[0][0] >= 3
            ):
                chosen = [with_bus[0][1]]

        for item in chosen:
            resolved = item["resolved"]
            result = item["result"]
            route = resolved["route"]
            terminal = route.get("to_station") or ""
            text = render_arrivals(
                line_token,
                terminal,
                resolved["station_name"],
                result,
                city=city_now,
            )
            if source == "inferred":
                text += f"\n🤔 {item['reason']}"
            chain = event.make_result().message(text)
            chain = await self.attach_map(
                chain,
                line_name=line_token,
                direction=item["direction"],
                station_order=resolved["station_order"],
                station_name=resolved["station_name"],
                direction_to=terminal,
                city=city_now,
                result=result,
                route=route,
            )
            yield chain

        if len(chosen) == 1:
            item = chosen[0]
            self._record_observation(
                umo,
                line=line_token,
                direction=int(item["direction"]),
                station=str(item["resolved"]["station_name"]),
                city=city_now,
                source=source,
                now=now,
            )

    # ---------------- 自然语言查询：LLM 函数工具 ----------------

    if hasattr(filter, "llm_tool"):

        @filter.llm_tool(name="bus_arrival")
        async def bus_arrival_tool(
            self,
            event: AstrMessageEvent,
            line_name: str,
            station: str = "",
            direction: str = "",
            city: str = "",
        ):
            """查询公交车的实时到站情况。

            当用户问某条公交线路「到哪了 / 还有几站 / 什么时候来 / 帮我查一下」时调用本工具。
            用户没提方向或候车站时**不要追问**，直接留空，本工具会按用户的历史习惯、
            通勤时段与当前位置自动推断。

            Args:
                line_name(string): 公交线路名，例如 "1路"、"K2路"。必填，从用户原话里提取。
                station(string): 候车站名或站序，例如 "市政府"。用户没说就传空字符串。
                direction(string): 行驶方向，可填 "去程" 或 "回程"，也可以直接填终点站名。
                    用户没有明确说方向时必须传空字符串，不要自己猜。
                city(string): 城市名。用户没说就传空字符串，本工具会用当前会话的城市。
            """
            if not self.nl_query:
                yield event.plain_result(
                    "自然语言查询已在插件配置里关闭，"
                    "可以用 /busnow <编号> 查订阅，或 /bushelp 看用法"
                )
                return
            async for chunk in self._answer_arrival(
                event, line_name, station, direction, city
            ):
                yield chunk

        @filter.llm_tool(name="bus_subscribe_add")
        async def bus_subscribe_add_tool(
            self,
            event: AstrMessageEvent,
            line_name: str,
            time: str,
            repeat: str,
            station: str = "",
            direction: str = "",
            city: str = "",
            top_n: str = "",
        ):
            """新建一条公交到站提醒（订阅）。

            当用户想「提醒我 / 帮我订 / 加一个闹钟 / 以后到点叫我」时调用本工具。
            时间与重复规则必须从用户原话里拿到；用户没说重复规则时默认传「每天」。
            用户没提方向或候车站时**不要追问**，直接留空，本工具会按习惯推断并在回执里说明。

            Args:
                line_name(string): 公交线路名，例如 "1路"、"K2路"。必填。
                time(string): 提醒时间，HH:MM 格式，例如 "07:30"。必填。
                repeat(string): 重复规则，可填 "每天"、"工作日"、"非工作日" 或 "12345"
                    （数字代表周几，1=周一）。用户没说就传 "每天"。
                station(string): 候车站名或站序，例如 "市政府" 或 "12"。用户没说就传空字符串。
                direction(string): 行驶方向，可填 "去程" 或 "回程"，也可以直接填终点站名。
                    用户没有明确说方向时必须传空字符串，不要自己猜。
                city(string): 城市名。用户没说就传空字符串，用当前会话的城市。
                top_n(string): 到点推送几辆车，纯数字。用户没说就传空字符串。
            """
            if not self.nl_subscribe:
                yield event.plain_result(
                    "自然语言设置订阅已在插件配置里关闭，"
                    "可以用 /busadd 添加，或 /bushelp 看用法"
                )
                return
            item, error, notes = await self._build_subscription(
                event,
                line_name=line_name,
                time_text=time,
                repeat_text=repeat,
                station=station,
                direction=direction,
                city=city,
                top_n=top_n,
            )
            if item is None:
                yield event.plain_result(f"⚠️ 没能建好这条订阅：{error}")
                return
            yield event.plain_result(self._sub_add_receipt(item, notes))

        @filter.llm_tool(name="bus_subscribe_list")
        async def bus_subscribe_list_tool(
            self,
            event: AstrMessageEvent,
            scope: str = "",
        ):
            """查看公交到站提醒（订阅）列表。

            当用户问「我订了哪些 / 有哪些提醒 / 看看我的订阅」时调用本工具。
            默认只列出当前用户自己创建的订阅。

            Args:
                scope(string): 查看范围。留空或 "mine" 表示只列自己的；
                    只有管理员可填 "all" 查看所有人的订阅。
            """
            if not self.nl_subscribe:
                yield event.plain_result(
                    "自然语言设置订阅已在插件配置里关闭，可以用 /buslist 查看"
                )
                return
            want_all = str(scope or "").strip().lower() in ("all", "全部", "所有人")
            if want_all and not self._is_admin(event):
                yield event.plain_result("🚫 只有管理员可以查看所有人的订阅")
                return
            if want_all:
                items = self.store.all()
                title = f"🚌 全部会话共有 {len(items)} 条订阅"
            else:
                items = self._own_items(event, event.unified_msg_origin)
                title = f"🚌 你在本会话有 {len(items)} 条订阅"
            if not items:
                yield event.plain_result(
                    title + "（还没有，直接说「每天早上7点半提醒我1路」就能加）"
                )
                return
            describer = self._describe_with_owner if want_all else self._describe
            body = "\n".join(describer(item) for item in items)
            yield event.plain_result(f"{title}：\n{body}")

        @filter.llm_tool(name="bus_subscribe_update")
        async def bus_subscribe_update_tool(
            self,
            event: AstrMessageEvent,
            sub_id: str,
            time: str = "",
            repeat: str = "",
            station: str = "",
            direction: str = "",
            line_name: str = "",
            city: str = "",
            top_n: str = "",
            enabled: str = "",
        ):
            """修改一条已有的公交到站提醒（订阅），也可以用它启用 / 暂停。

            当用户说「把 3 号改成 8 点 / 暂停 2 号 / 恢复 1 号 / 换成工作日」时调用本工具。
            用户没提到的字段一律留空，不要臆造。

            Args:
                sub_id(string): 要修改的订阅编号，例如 "3"，也接受 "[3]"、"#3"。
                time(string): 新的提醒时间 HH:MM。不改就传空字符串。
                repeat(string): 新的重复规则（每天 / 工作日 / 非工作日 / 12345）。不改就传空字符串。
                station(string): 新的候车站名或站序。不改就传空字符串。
                direction(string): 新的方向（去程 / 回程 / 终点站名）。不改就传空字符串。
                line_name(string): 新的线路名。不改就传空字符串。
                city(string): 新的城市名。不改就传空字符串。
                top_n(string): 新的显示车辆数。不改就传空字符串。
                enabled(string): 启用就填 "启用"，暂停就填 "暂停"。不改就传空字符串。
            """
            if not self.nl_subscribe:
                yield event.plain_result(
                    "自然语言设置订阅已在插件配置里关闭，"
                    "可以用 /buson、/busoff 或 /busdel，或 /bushelp 看用法"
                )
                return
            clean = self._clean_id(sub_id)
            item = self.store.get(clean) if clean else None
            if item is None:
                yield event.plain_result(f"没有找到编号为 {sub_id} 的订阅")
                return
            if not self._can_manage(event, item):
                yield event.plain_result(self._denied_text(clean, item))
                return

            changed, error = await self._apply_subscription_edit(
                event,
                item,
                time_text=time,
                repeat_text=repeat,
                station=station,
                direction=direction,
                line_name=line_name,
                city=city,
                top_n=top_n,
                enabled=enabled,
            )
            if error:
                yield event.plain_result(f"⚠️ 没能改这条订阅：{error}")
                return
            if not changed:
                yield event.plain_result(
                    f"没说要改什么，订阅 [{clean}] 保持原样：\n{self._describe(item)}"
                )
                return
            body = "\n".join(f"· {line}" for line in changed)
            yield event.plain_result(f"✏️ 已更新订阅 [{clean}]：\n{body}")

        @filter.llm_tool(name="bus_subscribe_remove")
        async def bus_subscribe_remove_tool(
            self,
            event: AstrMessageEvent,
            sub_id: str,
        ):
            """删除一条公交到站提醒（订阅）。

            当用户说「取消 / 删掉 / 不要了」某个订阅时调用本工具。

            Args:
                sub_id(string): 要删除的订阅编号，例如 "3"，也接受 "[3]"、"#3"。
            """
            if not self.nl_subscribe:
                yield event.plain_result(
                    "自然语言设置订阅已在插件配置里关闭，可以用 /busdel 删除"
                )
                return
            clean = self._clean_id(sub_id)
            item = self.store.get(clean) if clean else None
            if item is None:
                yield event.plain_result(f"没有找到编号为 {sub_id} 的订阅")
                return
            if not self._can_manage(event, item):
                yield event.plain_result(self._denied_text(clean, item))
                return
            self.store.remove(clean)
            yield event.plain_result(
                f"🗑 已删除订阅 [{clean}] {item.get('line_name')}"
                f" · {item.get('station_name')}"
            )

    async def build_map_image(
        self,
        line_name: str,
        direction: int,
        station_order: int,
        station_name: str,
        direction_to: str,
        city: str,
        result: dict,
        route: dict | None = None,
    ) -> str | None:
        """渲染「线路走向 + 车辆位置」地图，返回 PNG 的 base64 字符串。

        失败（线路没有经纬度、瓦片读不到、字体缺失等）时返回 None，
        由调用方决定退回纯文字推送。
        """
        try:
            # 惰性导入：Pillow 缺失时只影响地图，不阻断插件加载与页面显示
            from .map_render import render_route_map

            if route is None:
                route = await self.client.get_route(line_name, direction, city=city)
            title = (
                f"{line_name} · 往 {direction_to} · 候车 {station_name}"
                if direction_to
                else f"{line_name} · 候车 {station_name}"
            )
            png = await render_route_map(route, result, station_order, title)
        except Exception:
            logger.exception("渲染线路地图失败，本次只推送文字信息")
            return None
        return base64.b64encode(png).decode("ascii")

    async def attach_map(
        self,
        chain: Any,
        *,
        line_name: str,
        direction: int,
        station_order: int,
        station_name: str,
        direction_to: str,
        city: str,
        result: dict,
        route: dict | None = None,
    ) -> Any:
        """按配置把线路地图追加到消息链末尾（关闭或失败时原样返回）。"""
        if not self.map_in_push:
            return chain
        image_b64 = await self.build_map_image(
            line_name,
            direction,
            station_order,
            station_name,
            direction_to,
            city,
            result,
            route=route,
        )
        if image_b64:
            chain.base64_image(image_b64)
        return chain

    def _describe(self, item: dict) -> str:
        status = "✅" if item.get("enabled", True) else "⏸"
        direction_to = item.get("direction_to") or ""
        target = (
            f"往 {direction_to}" if direction_to else f"方向{item.get('direction')}"
        )
        city = str(item.get("city") or self.client.default_city)
        return (
            f"{status} [{item.get('id')}] {item.get('time')} · {describe_repeat(item.get('repeat'))}\n"
            f"     [{city}] {item.get('line_name')}（{target}）· 候车：{item.get('station_name')}"
            f" · 显示 {item.get('top_n', DEFAULT_TOP_N)} 辆"
        )

    # ---------------- 自然语言订阅管理：公共逻辑 ----------------

    @staticmethod
    def _clean_id(text: Any) -> str:
        """把「[3]」「#3」这类写法统一成纯数字编号。"""
        return "".join(ch for ch in str(text or "") if ch.isdigit())

    @staticmethod
    def _enabled_value(text: Any) -> bool | None:
        """把「启用 / 暂停 / true / false」解析成布尔值；无法识别返回 None。"""
        token = str(text or "").strip().lower()
        if not token or token == "none":
            return None
        if token in ("1", "true", "yes", "on", "启用", "开启", "打开", "恢复", "开始"):
            return True
        if token in ("0", "false", "no", "off", "暂停", "关闭", "停用", "停", "关"):
            return False
        return None

    async def _city_token(self, value: Any, fallback: str) -> str:
        """校验城市名（允许只写「福州」并自动补「市」），校验不了就原样返回。"""
        token = str(value or "").strip()
        if not token:
            return fallback
        exists = getattr(self.client, "city_exists", None)
        if exists is None:
            return token
        candidates = [token] + ([] if token.endswith("市") else [token + "市"])
        for candidate in candidates:
            try:
                if await exists(candidate):
                    return candidate
            except BusApiError:
                break
        return token

    def _find_duplicate(
        self,
        umo: str,
        line_name: str,
        direction: int,
        station_name: str,
        fire_time: str,
    ) -> dict | None:
        """查找完全重复的订阅（同会话 · 同线路 · 同方向 · 同站点 · 同时间）。"""
        for item in self.store.by_umo(umo):
            if (
                item.get("line_name") == line_name
                and int(item.get("direction") or 0) == int(direction)
                and item.get("time") == fire_time
                and self._same_station(
                    str(item.get("station_name") or ""), station_name
                )
            ):
                return item
        return None

    async def _build_subscription(
        self,
        event: AstrMessageEvent,
        *,
        line_name: str,
        time_text: str,
        repeat_text: str,
        station: str = "",
        direction: str = "",
        city: str = "",
        top_n: Any = "",
    ) -> tuple[dict | None, str, list[str]]:
        """把 AI 提取的参数整理成一条订阅。

        返回（订阅项, 错误说明, 推断提示）。订阅项为 None 时以错误说明为准；
        推断提示用于在回执里点名「哪些是我替你猜的」。
        """
        umo = event.unified_msg_origin
        notes: list[str] = []

        try:
            fire_time = self.parse_time(time_text)
        except ValueError as exc:
            return None, str(exc), notes
        try:
            repeat = parse_repeat(repeat_text)
        except RepeatRuleError as exc:
            return None, str(exc), notes

        line_token = (line_name or "").strip()
        if not line_token:
            return None, "要订阅哪条线路呢？例如「1路」", notes

        city_now = await self._city_token(city, self._city_for(event))
        try:
            lines = await self.client.search_lines(line_token, city=city_now)
        except BusApiError as exc:
            return None, f"查询线路失败：{exc}", notes

        exact = [line for line in lines if line["line_name"] == line_token]
        if not exact and len(lines) == 1:
            exact = lines
        if not exact:
            if not lines:
                return (
                    None,
                    (
                        f"在「{city_now}」没找到包含「{line_token}」的线路，"
                        "城市不对可以先用 /buscity 切换"
                    ),
                    notes,
                )
            body = "\n".join(
                f"· {line['line_name']}（方向{line['direction']}）"
                f"{line['from']} → {line['to']}"
                for line in lines[:10]
            )
            return (
                None,
                (f"「{line_token}」匹配到多条线路，请告诉我完整线路名：\n{body}"),
                notes,
            )

        line_token = exact[0]["line_name"]
        available = sorted({int(line["direction"]) for line in exact})
        now = dt.datetime.now()

        want = self._direction_from_text(direction, exact)
        if want in available:
            direction_now = want
        elif len(available) == 1:
            direction_now = available[0]
        else:
            inferred = await self._infer_direction(
                umo, line_token, city_now, station, available, now
            )
            if inferred is None:
                return (
                    None,
                    (
                        f"「{line_token}」有 {'、'.join(str(d) for d in available)} "
                        "两个方向，你没说走哪个（可以填「去程」「回程」或终点站名），"
                        "我也没从你的习惯里判断出来"
                    ),
                    notes,
                )
            direction_now = int(inferred[0])
            notes.append("方向：" + str(inferred[1]))

        kind = day_kind_of(now, self.holidays, self.workdays)
        provided = (station or "").strip()
        station_token = provided or self._station_hint(
            umo, line_token, direction_now, "", now, kind
        )
        if not station_token:
            return (
                None,
                "要在哪个站候车呢？说个站名或站序（例如「市政府」或「12」）",
                notes,
            )
        if not provided:
            notes.append(f"候车站：{station_token}")

        try:
            resolved = await self.resolve_station(
                line_token, direction_now, station_token, city_now
            )
        except BusApiError as exc:
            return None, f"查询线路失败：{exc}", notes
        except ValueError as exc:
            return None, str(exc), notes

        dup = self._find_duplicate(
            umo, line_token, direction_now, resolved["station_name"], fire_time
        )
        if dup is not None:
            return (
                None,
                (
                    f"你已经有一条一样的订阅了：[{dup['id']}] {dup['time']} "
                    f"{line_token} · 候车 {resolved['station_name']}"
                ),
                notes,
            )

        top = self.default_top_n
        if str(top_n or "").strip():
            try:
                top = max(1, int(str(top_n).strip()))
            except ValueError:
                top = self.default_top_n

        owner_id, owner_name = self._sender_identity(event)
        route = resolved["route"]
        item = self.store.add(
            {
                "umo": umo,
                "time": fire_time,
                "repeat": repeat,
                "city": city_now,
                "line_name": line_token,
                "direction": direction_now,
                "direction_to": route["to_station"],
                "station_order": resolved["station_order"],
                "station_name": resolved["station_name"],
                "top_n": top,
                "kind": "subscription",
                "owner_id": owner_id,
                "owner_name": owner_name,
            }
        )
        self._record_observation(
            umo,
            line=line_token,
            direction=direction_now,
            station=str(resolved["station_name"]),
            city=city_now,
            source="explicit",
            now=now,
        )
        return item, "", notes

    def _sub_add_receipt(self, item: dict, notes: list[str]) -> str:
        """新增订阅的回执；notes 非空时点名推断项。"""
        text = (
            f"✅ 已订阅 [{item['id']}]\n"
            f"⏰ {item['time']} · {describe_repeat(item.get('repeat'))}\n"
            f"🚏 [{item['city']}] {item['line_name']}"
            f"（往 {item.get('direction_to') or '未知'}）"
            f"· 候车：{item['station_name']}（第 {item.get('station_order')} 站）\n"
            f"📊 到点会推送最近的 {item.get('top_n')} 辆车"
        )
        if notes:
            text += "\n\n🤔 这些是我替你判断的，不对直接说，我帮你改：\n" + "\n".join(
                f"· {note}" for note in notes
            )
        return text

    def _describe_with_owner(self, item: dict) -> str:
        """在订阅描述后补一行创建者与会话（给管理员看全部订阅时用）。"""
        owner = str(item.get("owner_name") or item.get("owner_id") or "").strip()
        return (
            f"{self._describe(item)}\n"
            f"     👤 创建者：{owner or '（未记录）'} · 会话：{item.get('umo')}"
        )

    async def _apply_subscription_edit(
        self,
        event: AstrMessageEvent,
        item: dict,
        *,
        time_text: str = "",
        repeat_text: str = "",
        station: str = "",
        direction: str = "",
        line_name: str = "",
        city: str = "",
        top_n: Any = "",
        enabled: Any = "",
    ) -> tuple[list[str], str]:
        """把 AI 给出的字段应用到订阅上，返回（变更说明, 错误信息）。"""
        changed: list[str] = []

        if str(time_text or "").strip():
            try:
                item["time"] = self.parse_time(time_text)
            except ValueError as exc:
                return changed, str(exc)
            changed.append(f"提醒时间 → {item['time']}")

        if str(repeat_text or "").strip():
            try:
                item["repeat"] = parse_repeat(repeat_text)
            except RepeatRuleError as exc:
                return changed, str(exc)
            changed.append(f"重复规则 → {describe_repeat(item['repeat'])}")

        if str(enabled or "").strip():
            value = self._enabled_value(enabled)
            if value is None:
                return changed, "启用状态只认「启用」或「暂停」"
            item["enabled"] = value
            changed.append("状态 → 已启用" if value else "状态 → 已暂停")

        if str(top_n or "").strip():
            try:
                item["top_n"] = max(1, int(str(top_n).strip()))
            except ValueError:
                return changed, "显示车辆数要填数字"
            changed.append(f"显示车辆数 → {item['top_n']}")

        wants_route = any(
            str(value or "").strip() for value in (city, line_name, station, direction)
        )
        if wants_route:
            try:
                city_now = await self._city_token(
                    city, str(item.get("city") or self._city_for(event))
                )
                new_line = (line_name or item.get("line_name") or "").strip()
                lines = await self.client.search_lines(new_line, city=city_now)
                exact = [line for line in lines if line["line_name"] == new_line]
                if not exact and len(lines) == 1:
                    exact = lines
                current_dir = int(item.get("direction") or 1)
                available = sorted({int(line["direction"]) for line in exact}) or [
                    current_dir
                ]
                want = self._direction_from_text(direction, exact) if direction else 0
                if want in available:
                    new_dir = want
                elif str(direction or "").strip():
                    return changed, (
                        f"「{new_line}」没有这个方向，可选："
                        + "、".join(str(d) for d in available)
                    )
                else:
                    new_dir = current_dir if current_dir in available else available[0]
                new_station = (station or item.get("station_name") or "").strip()
                resolved = await self.resolve_station(
                    new_line, new_dir, new_station, city_now
                )
            except BusApiError as exc:
                return changed, f"查询线路失败：{exc}"
            except ValueError as exc:
                return changed, str(exc)

            route = resolved["route"]
            item.update(
                {
                    "city": city_now,
                    "line_name": route["line_name"],
                    "direction": new_dir,
                    "direction_to": route["to_station"],
                    "station_order": resolved["station_order"],
                    "station_name": resolved["station_name"],
                }
            )
            changed.append(
                f"线路 / 站点 → [{city_now}] {route['line_name']}"
                f"（往 {route['to_station']}）· 候车 {resolved['station_name']}"
            )

        if changed:
            self.store.save()
        return changed, ""

    # ---------------- 指令 ----------------

    @filter.command("bushelp")
    async def bushelp(self, event: AstrMessageEvent):
        """查看公交闹钟帮助"""
        yield event.plain_result(HELP_TEXT.format(city=self._city_for(event)))

    @filter.command("buscity")
    async def buscity(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """查看或切换城市。用法：/buscity [城市名] 或 /buscity list [关键词]"""
        current = self._city_for(event)
        token = arg1.strip()

        if not token:
            yield event.plain_result(
                f"📍 本会话当前城市：{current}\n\n"
                "· 切换城市：/buscity 福州\n"
                "· 搜索支持的城市：/buscity list 泉州\n"
                "· 插件默认城市在插件配置 default_city 里改"
            )
            return

        if token in ("list", "列表", "搜索"):
            keyword = arg2.strip()
            try:
                cities = await self.client.list_cities()
            except BusApiError as exc:
                yield event.plain_result(f"获取城市列表失败：{exc}")
                return
            if keyword:
                cities = [c for c in cities if keyword in c]
            if not cities:
                yield event.plain_result(f"没有找到含「{keyword}」的城市")
                return
            shown = cities[:40]
            text = (
                f"🏙 掌上公交支持 {len(cities)} 个城市"
                + (f"（含「{keyword}」）" if keyword else "")
                + "：\n"
                + "、".join(shown)
            )
            if len(cities) > len(shown):
                text += f"\n… 还有 {len(cities) - len(shown)} 个，加关键词缩小范围"
            text += "\n\n用 /buscity <城市名> 切换"
            yield event.plain_result(text)
            return

        # 切换城市：允许只写「福州」，会自动补「市」再校验
        candidates = [token]
        if not token.endswith("市"):
            candidates.append(token + "市")
        for candidate in candidates:
            try:
                exists = await self.client.city_exists(candidate)
            except BusApiError as exc:
                yield event.plain_result(f"查询城市失败：{exc}")
                return
            if exists:
                self.cities.set(event.unified_msg_origin, candidate)
                yield event.plain_result(
                    f"✅ 已把本会话城市切换为「{candidate}」，之后的 /buslines 与 /busadd 都按这个城市查询"
                )
                return

        try:
            cities = await self.client.list_cities()
        except BusApiError:
            cities = []
        similar = [c for c in cities if token.rstrip("市") and token.rstrip("市") in c][
            :10
        ]
        text = f"❌ 掌上公交暂不支持「{token}」"
        if similar:
            text += "\n你是不是想找：" + "、".join(similar)
        text += "\n用 /buscity list 关键词 搜索支持的城市"
        yield event.plain_result(text)

    @filter.command("buslines")
    async def buslines(
        self, event: AstrMessageEvent, keyword: str = "", station: str = ""
    ):
        """查看线路站点与站序。用法：/buslines <线路名> [站点关键词]"""
        if not keyword.strip():
            yield event.plain_result(
                "用法：/buslines <线路名> [站点关键词]\n"
                "例如：/buslines 1路 附小\n"
                "城市不对？先用 /buscity 切换"
            )
            return

        keyword = keyword.strip()
        city = self._city_for(event)
        try:
            lines = await self.client.search_lines(keyword, city=city)
        except BusApiError as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return

        if not lines:
            yield event.plain_result(
                f"在「{city}」没找到包含「{keyword}」的线路\n"
                "若城市不对，用 /buscity 切换；也可用 /buscity list 关键词 查看支持的城市"
            )
            return

        # 关键字精确命中线路时直接展示站点，否则列出候选线路
        exact = [line for line in lines if line["line_name"] == keyword]
        if not exact:
            text = f"🔍 {city} 包含「{keyword}」的线路共 {len(lines)} 条，请用完整线路名再试：\n"
            text += "\n".join(
                f"· {line['line_name']}（方向{line['direction']}）{line['from']} → {line['to']}"
                for line in lines[:14]
            )
            if len(lines) > 14:
                text += f"\n… 还有 {len(lines) - 14} 条"
            yield event.plain_result(text)
            return

        line = exact[0]
        try:
            route = await self.client.get_route(
                line["line_name"], line["direction"], city=city
            )
        except BusApiError as exc:
            yield event.plain_result(f"查询站点失败：{exc}")
            return

        stations = route["stations"]
        filter_token = station.strip()
        if filter_token:
            matched = [s for s in stations if filter_token in s["name"]]
            if not matched:
                yield event.plain_result(f"这条线上没有包含「{filter_token}」的站点")
                return
            body = "\n".join(f"  {s['order']:>3}. {s['name']}" for s in matched)
            head = f"🚏 [{city}] {route['line_name']}（往 {route['to_station']}）匹配「{filter_token}」的站点："
        else:
            shown = stations[:30]
            body = "\n".join(f"  {s['order']:>3}. {s['name']}" for s in shown)
            head = f"🚏 [{city}] {route['line_name']}（往 {route['to_station']}）共 {len(stations)} 站："
            if len(stations) > 30:
                body += f"\n  … 还有 {len(stations) - 30} 站，可加关键词过滤"

        yield event.plain_result(
            f"{head}\n{body}\n\n⏱ 首班 {route['first_time']} · 末班 {route['last_time']}"
        )

    @filter.command("busadd")
    async def busadd(
        self,
        event: AstrMessageEvent,
        time_text: str,
        repeat_text: str,
        line_name: str,
        direction: int,
        station: str,
        top_n: int = 0,
    ):
        """添加公交到站提醒。用法：/busadd 07:30 工作日 1路 1 附小 [车辆数]"""
        try:
            fire_time = self.parse_time(time_text)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        try:
            repeat = parse_repeat(repeat_text)
        except RepeatRuleError as exc:
            yield event.plain_result(str(exc))
            return

        if direction not in (1, 2):
            yield event.plain_result("方向只能是 1（上行）或 2（下行）")
            return

        city = self._city_for(event)
        try:
            resolved = await self.resolve_station(line_name, direction, station, city)
        except BusApiError as exc:
            yield event.plain_result(f"查询线路失败：{exc}")
            return
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        route = resolved["route"]
        owner_id, owner_name = self._sender_identity(event)
        item = self.store.add(
            {
                "umo": event.unified_msg_origin,
                "time": fire_time,
                "repeat": repeat,
                "city": city,
                "line_name": route["line_name"],
                "direction": direction,
                "direction_to": route["to_station"],
                "station_order": resolved["station_order"],
                "station_name": resolved["station_name"],
                "top_n": top_n if top_n > 0 else self.default_top_n,
                "kind": "subscription",
                "owner_id": owner_id,
                "owner_name": owner_name,
            }
        )

        yield event.plain_result(
            f"✅ 已添加提醒 [{item['id']}]\n"
            f"⏰ {fire_time} · {describe_repeat(repeat)}\n"
            f"🚏 [{city}] {item['line_name']}（往 {item['direction_to']}）· 候车：{item['station_name']}\n"
            f"📊 将显示最近的 {item['top_n']} 辆车\n\n"
            f"到达时间后会主动推送，可先用 /busnow {item['id']} 试一下"
        )

    @filter.command("buscalendar")
    async def buscalendar(self, event: AstrMessageEvent):
        """查看节假日 / 调休补班数据的来源与状态"""
        if self.calendar is None:
            yield event.plain_result(
                "在线节假日数据已在插件配置中关闭，"
                "「工作日 / 非工作日」将只按周末判断。\n"
                "如需恢复，请把配置项 holiday_api 打开。"
            )
            return

        await self.refresh_calendar_if_needed()
        today = dt.date.today()
        today_iso = today.isoformat()
        upcoming_holidays = sorted(d for d in self.holidays if d >= today_iso)[:6]
        upcoming_workdays = sorted(d for d in self.workdays if d >= today_iso)[:6]

        if today_iso in self.holidays:
            today_state = "休息日（法定节假日）"
        elif today_iso in self.workdays:
            today_state = "工作日（调休补班）"
        elif today.isoweekday() >= 6:
            today_state = "休息日（周末）"
        else:
            today_state = "工作日（周一至周五）"

        text = f"📅 节假日数据\n{self.calendar.describe()}\n今天 {today_iso}：{today_state}\n"
        if upcoming_holidays:
            text += f"\n接下来的放假日：{'、'.join(upcoming_holidays)}"
        if upcoming_workdays:
            text += f"\n接下来的补班日：{'、'.join(upcoming_workdays)}"
        if self._manual_holidays or self._manual_workdays:
            text += (
                f"\n\n手填补充：放假 {len(self._manual_holidays)} 天 / "
                f"补班 {len(self._manual_workdays)} 天"
            )
        yield event.plain_result(text)

    @filter.command("buslist")
    async def buslist(self, event: AstrMessageEvent):
        """查看我在本会话的公交订阅"""
        items = self.store.by_umo(event.unified_msg_origin)
        if not items:
            yield event.plain_result(
                "本会话还没有订阅，用 /busadd 添加，或 /bushelp 看用法"
            )
            return
        body = "\n".join(self._describe(item) for item in items)
        yield event.plain_result(f"🚌 本会话共有 {len(items)} 条订阅：\n{body}")

    @filter.command("busdel")
    async def busdel(self, event: AstrMessageEvent, sub_id: str):
        """删除公交订阅。用法：/busdel <编号>"""
        item = self.store.get(sub_id)
        if item is None or item.get("umo") != event.unified_msg_origin:
            yield event.plain_result(f"没有找到编号为 {sub_id} 的订阅")
            return
        if not self._can_manage(event, item):
            yield event.plain_result(self._denied_text(sub_id, item))
            return
        self.store.remove(sub_id)
        yield event.plain_result(
            f"🗑 已删除订阅 [{sub_id}] {item.get('line_name')} · {item.get('station_name')}"
        )

    @filter.command("buson")
    async def buson(self, event: AstrMessageEvent, sub_id: str):
        """启用公交订阅。用法：/buson <编号>"""
        item = self.store.get(sub_id)
        if item is None or item.get("umo") != event.unified_msg_origin:
            yield event.plain_result(f"没有找到编号为 {sub_id} 的订阅")
            return
        if not self._can_manage(event, item):
            yield event.plain_result(self._denied_text(sub_id, item))
            return
        if not item.get("enabled", True):
            self.store.toggle(sub_id)
        yield event.plain_result(f"✅ 订阅 [{sub_id}] 已启用")

    @filter.command("busoff")
    async def busoff(self, event: AstrMessageEvent, sub_id: str):
        """暂停公交订阅。用法：/busoff <编号>"""
        item = self.store.get(sub_id)
        if item is None or item.get("umo") != event.unified_msg_origin:
            yield event.plain_result(f"没有找到编号为 {sub_id} 的订阅")
            return
        if not self._can_manage(event, item):
            yield event.plain_result(self._denied_text(sub_id, item))
            return
        if item.get("enabled", True):
            self.store.toggle(sub_id)
        yield event.plain_result(f"⏸ 订阅 [{sub_id}] 已暂停")

    @filter.command("busnow")
    async def busnow(self, event: AstrMessageEvent, sub_id: str):
        """立即查询一次订阅的到站情况。用法：/busnow <编号>"""
        item = self.store.get(sub_id)
        if item is None or item.get("umo") != event.unified_msg_origin:
            yield event.plain_result(f"没有找到编号为 {sub_id} 的订阅")
            return

        city = str(item.get("city") or self.client.default_city)
        try:
            result = await self.client.query(
                item["line_name"],
                int(item["direction"]),
                int(item["station_order"]),
                int(item.get("top_n") or self.default_top_n),
                city=city,
            )
        except BusApiError as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return

        chain = event.make_result().message(
            render_arrivals(
                item["line_name"],
                item.get("direction_to") or "",
                item.get("station_name") or "",
                result,
                city=city,
            )
        )
        chain = await self.attach_map(
            chain,
            line_name=item["line_name"],
            direction=int(item["direction"]),
            station_order=int(item["station_order"]),
            station_name=item.get("station_name") or "",
            direction_to=item.get("direction_to") or "",
            city=city,
            result=result,
        )
        yield chain

    @filter.command("bushabit")
    async def bushabit(self, event: AstrMessageEvent, action: str = ""):
        """查看插件学到的出行习惯。用法：/bushabit [clear]"""
        umo = event.unified_msg_origin
        if action.strip().lower() in ("clear", "清空", "重置", "reset"):
            self.profile.clear(umo, places=False, observations=True)
            yield event.plain_result(
                "🧹 已清空出行记录。家 / 公司的地点记忆还在，可用 /busplace clear 清除"
            )
            return

        stats = self.profile.stats(umo)
        yield event.plain_result(
            f"{self.profile.describe(umo)}\n\n"
            f"📊 出行记录 {stats['observations']} 条"
            f" · 家 {'✓' if stats['home'] else '—'}"
            f" · 公司 {'✓' if stats['work'] else '—'}"
            f" · 常去 {stats['frequent']} 个\n"
            "· 清空记录：/bushabit clear\n"
            "· 设置家 / 公司：/busplace home 市政府"
        )

    @filter.command("busplace")
    async def busplace(self, event: AstrMessageEvent, action: str = "", name: str = ""):
        """查看 / 设置地点记忆。用法：/busplace [home|work|frequent 站名] 或 /busplace clear [类型]"""
        umo = event.unified_msg_origin
        action = (action or "").strip()
        name = (name or "").strip()

        if not action:
            yield event.plain_result(
                f"{self._describe_places(umo)}\n\n"
                "设置：/busplace home 市政府\n"
                "     /busplace work 附小\n"
                "清除：/busplace clear home  或  /busplace clear"
            )
            return

        if action.lower() in ("clear", "清空"):
            role = self._place_role(name)
            if role:
                removed = self.profile.remove_place(umo, role)
                yield event.plain_result(
                    f"🧹 已清除{self._place_label(role)}的站点记忆"
                    if removed
                    else f"本来就没有记录{self._place_label(role)}"
                )
                return
            for item in ("home", "work", "frequent"):
                self.profile.remove_place(umo, item)
            yield event.plain_result("🧹 已清空全部地点记忆")
            return

        role = self._place_role(action)
        if role is None:
            yield event.plain_result(
                "地点类型只能是 home（家）/ work（公司）/ frequent（常去）"
            )
            return
        if not name:
            yield event.plain_result(
                f"请在后面跟上站名，例如：/busplace {action} 市政府"
            )
            return

        record = self.profile.set_place(umo, role, name, city=self._city_for(event))
        if record is None:
            yield event.plain_result("站名不能为空")
            return
        yield event.plain_result(
            f"✅ 已记住：{self._place_label(role)} = {record['name']}\n"
            "之后问「XX路到哪了」时，插件会参考这些地点来判断方向"
        )

    @staticmethod
    def _place_role(token: str) -> str | None:
        lowered = (token or "").strip().lower()
        if lowered in ("home", "家", "住家", "住所", "我家"):
            return "home"
        if lowered in ("work", "公司", "单位", "上班", "公司站"):
            return "work"
        if lowered in ("frequent", "常去", "常去地点"):
            return "frequent"
        return None

    @staticmethod
    def _place_label(role: str) -> str:
        return {"home": "家", "work": "公司", "frequent": "常去"}.get(role, role)

    def _describe_places(self, umo: str) -> str:
        places = self.profile.places(umo)
        home = places.get("home")
        work = places.get("work")
        lines = [
            "🗺 地点记忆",
            f"  · 家：{home['name']}（{home['source']}）" if home else "  · 家：还没有",
            f"  · 公司：{work['name']}（{work['source']}）"
            if work
            else "  · 公司：还没有",
        ]
        frequent = places.get("frequent") or []
        if frequent:
            names = "、".join(str(item.get("name")) for item in frequent)
            lines.append(f"  · 常去：{names}")
        return "\n".join(lines)

    @filter.command("busstop")
    async def busstop(self, event: AstrMessageEvent):
        """查询并推送一次（测试主动推送是否可用）"""
        items = self.store.by_umo(event.unified_msg_origin)
        if not items:
            yield event.plain_result("本会话还没有订阅，先用 /busadd 添加")
            return
        item = items[0]
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain().message(self._describe(item) + "\n（这是主动推送测试）"),
            )
        except Exception as exc:
            logger.exception("主动推送测试失败")
            yield event.plain_result(f"主动推送失败：{exc}")
            return
        yield event.plain_result(
            "已发送一条主动推送，若未收到请检查平台是否支持主动消息"
        )
