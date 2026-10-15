"""定时调度：每分钟检查一次订阅，命中即推送实时到站信息。"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from astrbot.api.event import MessageChain

from .alarm_store import AlarmStore
from .bus_api import BusApiError, render_arrivals

logger = logging.getLogger("astrbot")

TICK_SECONDS = 30


class AlarmScheduler:
    """插件内嵌的轻量调度器（不依赖外部调度库）。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._task: asyncio.Task | None = None

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="bus_alarm_scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        # 整个循环内部消化异常，确保调度器不会因为一次失败而退出
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("公交闹钟调度出现异常")

    # ---------------- 单次检查 ----------------

    async def _tick(self) -> None:
        store: AlarmStore = self.plugin.store
        now = dt.datetime.now()
        await self.plugin.refresh_calendar_if_needed()
        holidays = self.plugin.holidays
        workdays = self.plugin.workdays

        for item in store.due(now, holidays, workdays):
            sub_id = str(item.get("id"))
            # 先落盘触发日期，避免推送失败时被反复触发
            store.mark_fired(sub_id, now.date().isoformat())
            try:
                await self._fire(item)
            except Exception:
                logger.exception("推送订阅 %s 失败", sub_id)

    async def _fire(self, item: dict) -> None:
        line_name = item.get("line_name") or ""
        direction = int(item.get("direction") or 1)
        station_order = int(item.get("station_order") or 1)
        station_name = item.get("station_name") or f"站序{station_order}"
        top_n = int(item.get("top_n") or self.plugin.default_top_n)
        # 订阅自带城市；老数据没有就退回插件默认城市
        city = str(item.get("city") or self.plugin.client.default_city)

        try:
            result = await self.plugin.client.query(
                line_name, direction, station_order, top_n, city=city
            )
        except BusApiError as exc:
            text = f"🚌 [{city}] {line_name} · 候车：{station_name}\n⚠️ 查询失败：{exc}"
        else:
            text = render_arrivals(
                line_name,
                item.get("direction_to") or "",
                station_name,
                result,
                city=city,
            )

        umo = item.get("umo")
        if not umo:
            logger.warning("订阅 %s 缺少会话标识，无法推送", item.get("id"))
            return
        try:
            await self.plugin.context.send_message(umo, MessageChain().message(text))
        except Exception:
            logger.exception("向 %s 发送公交信息失败", umo)
