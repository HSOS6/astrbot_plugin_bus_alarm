"""在线节假日 / 调休补班日数据。

主数据源用 NateScarlet/holiday-cn（每年一份静态 JSON，由 jsDelivr CDN 分发，
内容依据国务院办公厅的放假通知文件），备用 timor.tech。

拿到的是两个日期集合：
    holidays  放假日（该天休息）
    workdays  调休补班日（周末但要上班）
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path

import httpx

logger = logging.getLogger("astrbot")

HOLIDAY_CN_URL = "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
TIMOR_URL = "https://timor.tech/api/holiday/year/{year}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


class HolidayCalendar:
    """节假日日历：拉取在线数据 + 本地缓存。"""

    CACHE_FILENAME = "holidays.json"

    def __init__(
        self, data_dir: Path, timeout: float = 10.0, cache_days: int = 7
    ) -> None:
        self.path = Path(data_dir) / self.CACHE_FILENAME
        self.cache_days = max(1, int(cache_days))
        self.timeout = timeout

        self.holidays: set[str] = set()
        self.workdays: set[str] = set()
        self.source = ""
        self.fetched_at: dt.datetime | None = None

        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=_HEADERS
        )
        self._lock = asyncio.Lock()

    # ---------------- 缓存 ----------------

    def load_cache(self) -> None:
        """从磁盘恢复缓存；文件不存在或损坏时静默跳过。"""
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, ValueError) as exc:
            logger.warning("读取节假日缓存失败：%s", exc)
            return

        years = payload.get("years") or {}
        holidays: set[str] = set()
        workdays: set[str] = set()
        for entry in years.values():
            holidays.update(entry.get("holidays") or [])
            workdays.update(entry.get("workdays") or [])

        self.holidays = holidays
        self.workdays = workdays
        self.source = str(payload.get("source") or "")
        raw_time = payload.get("fetched_at") or ""
        try:
            self.fetched_at = dt.datetime.fromisoformat(raw_time) if raw_time else None
        except ValueError:
            self.fetched_at = None

    def _save_cache(self, years: dict[int, dict[str, list[str]]]) -> None:
        payload = {
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": self.source,
            "years": {str(year): data for year, data in years.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self.path.parent),
                delete=False,
                suffix=".tmp",
            ) as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
                tmp_path = fp.name
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logger.warning("写入节假日缓存失败：%s", exc)

    # ---------------- 刷新 ----------------

    def _is_stale(self) -> bool:
        if self.fetched_at is None:
            return True
        age = dt.datetime.now() - self.fetched_at
        return age.days >= self.cache_days

    async def ensure_fresh(self, force: bool = False) -> bool:
        """缓存过期时拉取在线数据。返回是否发生了更新。"""
        async with self._lock:
            if not force and not self._is_stale():
                return False

            today = dt.date.today()
            wanted = {today.year, today.year + 1}
            fetched: dict[int, dict[str, list[str]]] = {}

            for year in sorted(wanted):
                data = await self._fetch_year(year)
                if data and data["holidays"]:
                    fetched[year] = data
                    logger.info(
                        "已获取 %d 年节假日数据：放假 %d 天 / 补班 %d 天（来源 %s）",
                        year,
                        len(data["holidays"]),
                        len(data["workdays"]),
                        self.source,
                    )
                else:
                    # 次年的通知通常到年底才发布，拿不到就先跳过
                    logger.debug("%d 年节假日数据暂不可用，跳过", year)

            if not fetched:
                logger.warning("未能获取任何在线节假日数据，暂时只能按周末判断")
                # 记录尝试时间，避免每次 tick 都去请求
                self.fetched_at = dt.datetime.now()
                self._save_cache({})
                return False

            holidays: set[str] = set()
            workdays: set[str] = set()
            for data in fetched.values():
                holidays.update(data["holidays"])
                workdays.update(data["workdays"])

            self.holidays = holidays
            self.workdays = workdays
            self.fetched_at = dt.datetime.now()
            self._save_cache(fetched)
            return True

    async def _fetch_year(self, year: int) -> dict[str, list[str]] | None:
        """先试 holiday-cn，失败再试 timor.tech。"""
        for name, fetch in (
            ("holiday-cn", self._from_holiday_cn),
            ("timor.tech", self._from_timor),
        ):
            try:
                data = await fetch(year)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.debug("%s 获取 %d 年数据失败：%s", name, year, exc)
                continue
            if data and data["holidays"]:
                self.source = name
                return data
        return None

    async def _from_holiday_cn(self, year: int) -> dict[str, list[str]]:
        resp = await self._client.get(HOLIDAY_CN_URL.format(year=year))
        resp.raise_for_status()
        days = (resp.json() or {}).get("days") or []
        holidays = [d["date"] for d in days if d.get("isOffDay")]
        workdays = [d["date"] for d in days if not d.get("isOffDay")]
        return {"holidays": holidays, "workdays": workdays}

    async def _from_timor(self, year: int) -> dict[str, list[str]]:
        resp = await self._client.get(TIMOR_URL.format(year=year))
        resp.raise_for_status()
        payload = resp.json() or {}
        if int(payload.get("code") or 0) != 0:
            return {"holidays": [], "workdays": []}
        holidays: list[str] = []
        workdays: list[str] = []
        for item in (payload.get("holiday") or {}).values():
            date = item.get("date")
            if not date:
                continue
            (holidays if item.get("holiday") else workdays).append(date)
        return {"holidays": holidays, "workdays": workdays}

    async def close(self) -> None:
        await self._client.aclose()

    # ---------------- 描述 ----------------

    def describe(self) -> str:
        if not self.fetched_at:
            return "尚未获取在线节假日数据（仅按周末判断工作日）"
        stamp = self.fetched_at.strftime("%Y-%m-%d %H:%M")
        return (
            f"来源 {self.source or '未知'} · 更新于 {stamp}\n"
            f"本年度已收录：放假 {len(self.holidays)} 天 / 调休补班 {len(self.workdays)} 天"
        )
