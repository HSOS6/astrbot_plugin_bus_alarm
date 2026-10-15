"""三明公交赶车闹钟。

到达设定时间后，主动推送候车站最近数辆公交车的到站信息。
数据源为三明「掌上公交」H5 接口（h5.mygolbs.com）。

指令：
    /buslines <线路名> [站点关键词]   查看线路站点与站序
    /busadd <HH:MM> <重复> <线路名> <方向> <站序或站名> [车辆数]
                                      添加订阅
    /buslist                          查看我的订阅
    /busdel <编号>                    删除订阅
    /buson  <编号>                    启用订阅
    /busoff <编号>                    暂停订阅
    /busnow <编号>                    立即查询一次
    /buscalendar                      查看节假日 / 补班日数据
    /bushelp                          查看帮助
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .alarm_store import (
    AlarmStore,
    RepeatRuleError,
    _split_dates,
    describe_repeat,
    parse_repeat,
)
from .bus_api import CITY_KEY, CITY_NAME, BusApiError, BusClient, render_arrivals
from .holiday_api import HolidayCalendar
from .scheduler import AlarmScheduler

logger = logging.getLogger("astrbot")

PLUGIN_NAME = "astrbot_plugin_sm_bus_alarm"
DEFAULT_TOP_N = 3

HELP_TEXT = (
    "🚌 三明公交赶车闹钟\n"
    "——————————————\n"
    "① 先查站序\n"
    "   /buslines 1路 附小\n"
    "② 再加订阅\n"
    "   /busadd 07:30 工作日 1路 1 附小 3\n"
    "   ⤷ 时间 HH:MM · 重复规则 · 线路 · 方向 · 站序或站名 · 显示车辆数\n"
    "   ⤷ 重复规则可填：每天 / 工作日 / 非工作日 / 12345(周几，1=周一)\n"
    "——————————————\n"
    "/buslist         查看我的订阅\n"
    "/busdel  <编号>  删除订阅\n"
    "/buson   <编号>  启用订阅\n"
    "/busoff  <编号>  暂停订阅\n"
    "/busnow  <编号>  立即查询一次\n"
    "/buscalendar     查看节假日 / 补班日数据"
)


class SmBusAlarm(Star):
    """公交赶车闹钟插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        self.client = BusClient(
            city_key=self._cfg("city_key", CITY_KEY) or CITY_KEY,
            city_name=self._cfg("city_name", CITY_NAME) or CITY_NAME,
            timeout=float(self._cfg("request_timeout", 10) or 10),
        )

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = AlarmStore(data_dir)
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
            self._sync_calendar()
            # 拉取在线数据可能耗时，放到后台，不阻塞插件加载
            self._calendar_task = asyncio.create_task(
                self._refresh_calendar(), name="sm_bus_alarm_calendar"
            )
        self.scheduler.start()
        logger.info(
            "三明公交赶车闹钟已启动，当前 %d 条订阅，数据目录：%s\n节假日数据：%s",
            len(self.store.all()),
            self.store.path.parent,
            self.calendar.describe() if self.calendar else "已关闭在线数据，仅按周末判断",
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

    def _sync_calendar(self) -> None:
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
                self._sync_calendar()
        except Exception:
            logger.exception("刷新节假日数据失败")

    async def _refresh_calendar(self) -> None:
        await self.refresh_calendar_if_needed()

    # ---------------- 工具方法 ----------------

    @staticmethod
    def _parse_time(text: str) -> str:
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

    async def _resolve_station(
        self, line_name: str, direction: int, station: str
    ) -> dict:
        """把用户输入的站序或站名解析成站点信息。"""
        route = await self.client.get_route(line_name, direction)
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

    def _describe(self, item: dict) -> str:
        status = "✅" if item.get("enabled", True) else "⏸"
        direction_to = item.get("direction_to") or ""
        target = (
            f"往 {direction_to}" if direction_to else f"方向{item.get('direction')}"
        )
        return (
            f"{status} [{item.get('id')}] {item.get('time')} · {describe_repeat(item.get('repeat'))}\n"
            f"     {item.get('line_name')}（{target}）· 候车：{item.get('station_name')}"
            f" · 显示 {item.get('top_n', DEFAULT_TOP_N)} 辆"
        )

    # ---------------- 指令 ----------------

    @filter.command("bushelp")
    async def bushelp(self, event: AstrMessageEvent):
        """查看公交闹钟帮助"""
        yield event.plain_result(HELP_TEXT)

    @filter.command("buslines")
    async def buslines(
        self, event: AstrMessageEvent, keyword: str = "", station: str = ""
    ):
        """查看线路站点与站序。用法：/buslines 1路 [站点关键词]"""
        if not keyword.strip():
            yield event.plain_result(
                "用法：/buslines <线路名> [站点关键词]\n例如：/buslines 1路 附小"
            )
            return

        keyword = keyword.strip()
        try:
            lines = await self.client.search_lines(keyword)
        except BusApiError as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return

        if not lines:
            yield event.plain_result(f"没找到包含「{keyword}」的线路")
            return

        # 关键字精确命中线路时直接展示站点，否则列出候选线路
        exact = [line for line in lines if line["line_name"] == keyword]
        if not exact:
            text = (
                f"🔍 包含「{keyword}」的线路共 {len(lines)} 条，请用完整线路名再试：\n"
            )
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
            route = await self.client.get_route(line["line_name"], line["direction"])
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
            head = f"🚏 {route['line_name']}（往 {route['to_station']}）匹配「{filter_token}」的站点："
        else:
            shown = stations[:30]
            body = "\n".join(f"  {s['order']:>3}. {s['name']}" for s in shown)
            head = f"🚏 {route['line_name']}（往 {route['to_station']}）共 {len(stations)} 站："
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
            fire_time = self._parse_time(time_text)
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

        try:
            resolved = await self._resolve_station(line_name, direction, station)
        except BusApiError as exc:
            yield event.plain_result(f"查询线路失败：{exc}")
            return
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        route = resolved["route"]
        item = self.store.add(
            {
                "umo": event.unified_msg_origin,
                "time": fire_time,
                "repeat": repeat,
                "line_name": route["line_name"],
                "direction": direction,
                "direction_to": route["to_station"],
                "station_order": resolved["station_order"],
                "station_name": resolved["station_name"],
                "top_n": top_n if top_n > 0 else self.default_top_n,
                "kind": "subscription",
            }
        )

        yield event.plain_result(
            f"✅ 已添加提醒 [{item['id']}]\n"
            f"⏰ {fire_time} · {describe_repeat(repeat)}\n"
            f"🚌 {item['line_name']}（往 {item['direction_to']}）· 候车：{item['station_name']}\n"
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

        try:
            result = await self.client.query(
                item["line_name"],
                int(item["direction"]),
                int(item["station_order"]),
                int(item.get("top_n") or self.default_top_n),
            )
        except BusApiError as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return

        yield event.plain_result(
            render_arrivals(
                item["line_name"],
                item.get("direction_to") or "",
                item.get("station_name") or "",
                result,
            )
        )

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
