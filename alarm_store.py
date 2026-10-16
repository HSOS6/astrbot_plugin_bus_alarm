"""订阅数据的持久化与日期规则解析。

数据存放在 AstrBot 的 data/plugin_data/<插件名>/ 目录下（由 StarTools.get_data_dir 提供），
避免插件更新时被覆盖。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from astrbot.api import logger

RULE_EVERYDAY = "everyday"
RULE_WORKDAY = "workday"
RULE_RESTDAY = "restday"
RULE_WEEKDAYS = "weekdays"

WEEKDAY_NAMES = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}

_RULE_ALIASES = {
    "每天": RULE_EVERYDAY,
    "每日": RULE_EVERYDAY,
    "everyday": RULE_EVERYDAY,
    "daily": RULE_EVERYDAY,
    "all": RULE_EVERYDAY,
    "工作日": RULE_WORKDAY,
    "仅工作日": RULE_WORKDAY,
    "workday": RULE_WORKDAY,
    "weekday": RULE_WORKDAY,
    "非工作日": RULE_RESTDAY,
    "休息日": RULE_RESTDAY,
    "节假日": RULE_RESTDAY,
    "restday": RULE_RESTDAY,
    "weekend": RULE_RESTDAY,
}


class RepeatRuleError(ValueError):
    """重复规则解析失败。"""


def parse_repeat(text: str) -> dict:
    """把用户输入的重复规则解析成结构化数据。

    支持：每天 / 工作日 / 非工作日 / 12345（1=周一 … 7=周日，可任意组合）
    """
    raw = (text or "").strip()
    if not raw:
        raise RepeatRuleError("重复规则不能为空")

    lowered = raw.lower()
    if lowered in _RULE_ALIASES:
        return {"type": _RULE_ALIASES[lowered]}

    if lowered in ("weekdays12345",) or raw in ("周一至周五", "星期一到星期五"):
        return {"type": RULE_WEEKDAYS, "days": [1, 2, 3, 4, 5]}

    days = sorted({int(ch) for ch in raw if ch.isdigit() and 1 <= int(ch) <= 7})
    if days:
        return {"type": RULE_WEEKDAYS, "days": days}

    raise RepeatRuleError("无法识别的重复规则，可用：每天 / 工作日 / 非工作日 / 12345")


def describe_repeat(rule: dict) -> str:
    """把重复规则渲染成中文描述。"""
    rule_type = (rule or {}).get("type")
    if rule_type == RULE_EVERYDAY:
        return "每天"
    if rule_type == RULE_WORKDAY:
        return "工作日"
    if rule_type == RULE_RESTDAY:
        return "非工作日"
    if rule_type == RULE_WEEKDAYS:
        days = sorted(rule.get("days") or [])
        # 注意：这里只按星期判断，不含节假日 / 调休逻辑，
        # 所以不能显示成「工作日」「非工作日」，以免和同名规则混淆
        if days == [1, 2, 3, 4, 5]:
            return "周一至周五"
        if days == [6, 7]:
            return "周六、周日"
        return "、".join(WEEKDAY_NAMES.get(d, str(d)) for d in days) or "每天"
    return "每天"


def _split_dates(text: str) -> set[str]:
    """把 "2026-01-01,2026-01-02" 这样的配置解析成日期字符串集合。"""
    items: set[str] = set()
    for chunk in (text or "").replace("，", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            items.add(dt.date.fromisoformat(chunk).isoformat())
        except ValueError:
            logger.warning("忽略无法解析的日期配置项：%s", chunk)
    return items


def is_due_today(
    rule: dict, today: dt.date, holidays: set[str], workdays: set[str]
) -> bool:
    """判断某条订阅的重复规则在 today 是否应当触发。

    holidays 为法定节假日，workdays 为调休上班日（均为 YYYY-MM-DD）。
    """
    iso = today.isoformat()
    is_weekend = today.isoweekday() >= 6
    # 调休上班日视为工作日，法定节假日视为休息日
    is_workday = (not is_weekend and iso not in holidays) or iso in workdays
    is_restday = (is_weekend and iso not in workdays) or iso in holidays

    rule_type = (rule or {}).get("type", RULE_EVERYDAY)
    if rule_type == RULE_EVERYDAY:
        return True
    if rule_type == RULE_WORKDAY:
        return is_workday
    if rule_type == RULE_RESTDAY:
        return is_restday
    if rule_type == RULE_WEEKDAYS:
        return today.isoweekday() in set(rule.get("days") or [])
    return True


def can_manage(
    item: dict | None, sender_id: str, *, is_admin: bool = False, umo: str = ""
) -> bool:
    """判断某个操作者能否查看 / 修改 / 删除这条订阅。

    规则：
      - 管理员（is_admin）可以管理所有人的订阅；
      - 有创建者记录（owner_id）时，只有创建者本人能操作；
      - 早期数据（本功能上线前创建）没有 owner_id，退回到「同会话可管理」，
        否则升级后老订阅谁都动不了。
    """
    if is_admin:
        return True
    if not item:
        return False
    owner = str(item.get("owner_id") or "")
    if owner:
        return owner == str(sender_id or "")
    return bool(umo) and item.get("umo") == umo


class AlarmStore:
    """订阅的增删查改，JSON 文件持久化。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "subscriptions.json"
        self._items: dict[str, dict] = {}
        self._next_id = 1
        self.load()

    # ---------------- 读写 ----------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, ValueError) as exc:
            logger.error("读取订阅文件失败：%s", exc)
            return

        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, dict):
            self._items = {
                str(key): value
                for key, value in items.items()
                if isinstance(value, dict)
            }
            self._next_id = int(payload.get("next_id") or 1)
        elif isinstance(items, list):
            # 兼容早期以列表存储的格式
            self._items = {str(index + 1): value for index, value in enumerate(items)}
            self._next_id = len(self._items) + 1

    def save(self) -> None:
        payload = {"next_id": self._next_id, "items": self._items}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再替换，避免写入过程中被中断导致文件损坏
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
            logger.error("保存订阅文件失败：%s", exc)

    # ---------------- 查询 ----------------

    def all(self) -> list[dict]:
        return list(self._items.values())

    def get(self, sub_id: str) -> dict | None:
        return self._items.get(str(sub_id))

    def by_umo(self, umo: str) -> list[dict]:
        return [item for item in self._items.values() if item.get("umo") == umo]

    def due(
        self, now: dt.datetime, holidays: set[str], workdays: set[str]
    ) -> list[dict]:
        """返回此刻应当触发的订阅。"""
        current = now.strftime("%H:%M")
        today_iso = now.date().isoformat()
        result: list[dict] = []
        for item in self._items.values():
            if not item.get("enabled", True):
                continue
            if item.get("time") != current:
                continue
            if item.get("last_fire_date") == today_iso:
                continue
            if not is_due_today(
                item.get("repeat") or {}, now.date(), holidays, workdays
            ):
                continue
            result.append(item)
        return result

    # ---------------- 修改 ----------------

    def add(self, payload: dict[str, Any]) -> dict:
        sub_id = str(self._next_id)
        self._next_id += 1
        item = {"id": sub_id, "enabled": True, "last_fire_date": "", **payload}
        self._items[sub_id] = item
        self.save()
        return item

    def remove(self, sub_id: str) -> bool:
        if str(sub_id) not in self._items:
            return False
        del self._items[str(sub_id)]
        self.save()
        return True

    def toggle(self, sub_id: str) -> bool | None:
        item = self.get(sub_id)
        if item is None:
            return None
        item["enabled"] = not item.get("enabled", True)
        self.save()
        return bool(item["enabled"])

    def mark_fired(self, sub_id: str, date_iso: str) -> None:
        item = self.get(sub_id)
        if item is None:
            return
        item["last_fire_date"] = date_iso
        self.save()


class CityStore:
    """会话级默认城市（umo → 城市名），JSON 持久化。

    订阅本身也会记下自己的城市，所以用户之后换城市不影响已有提醒。
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "session_cities.json"
        self._items: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, ValueError) as exc:
            logger.error("读取会话城市文件失败：%s", exc)
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, dict):
            self._items = {str(k): str(v) for k, v in items.items() if v}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self.path.parent),
                delete=False,
                suffix=".tmp",
            ) as fp:
                json.dump({"items": self._items}, fp, ensure_ascii=False, indent=2)
                tmp_path = fp.name
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logger.error("保存会话城市文件失败：%s", exc)

    def get(self, umo: str, default: str) -> str:
        return self._items.get(str(umo)) or default

    def set(self, umo: str, city: str) -> None:
        self._items[str(umo)] = city
        self.save()
