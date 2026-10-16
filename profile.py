"""用户出行画像：出行观测记录 + 地点记忆（家 / 公司 / 常去）。

自然语言查询要在用户「没说方向」时也能猜对，靠的就是这里积累的数据：

1. **出行观测**：每次查询（不管用户明说还是插件推断）都记一条
   「什么时候 · 工作日还是休息日 · 哪条线 · 哪个方向 · 哪个站」。
   用户**明说**的方向权重高（1.0），插件**推断**出来的权重低（0.3），
   避免猜错一次之后自我强化。
   一天按 30 分钟切成 48 个桶，通勤 4 次（早去 / 午回 / 午去 / 晚回）
   天然落在 4 个桶里，互不干扰。

2. **地点记忆**：家 / 公司 / 常去。来源分 `explicit`（用户明说，永不覆盖）
   与 `inferred`（插件从「早上从 A 出发、傍晚从 B 出发且方向相反」推断）。

数据存放在 AstrBot 的 data/plugin_data/<插件名>/profile.json。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

from astrbot.api import logger

# 一天切成 48 个 30 分钟的时间桶
BUCKET_MINUTES = 30
BUCKETS_PER_DAY = 24 * 60 // BUCKET_MINUTES

# 认家 / 认公司的学习窗口（分钟数，从 0 点算起）
MORNING_WINDOW = (6 * 60, 10 * 60)
EVENING_WINDOW = (16 * 60, 20 * 60)

# 同一条观测在几分钟内重复出现时不重复记录
_DEDUPE_MINUTES = 10
# 时间衰减半衰期（天）：越近的记录越有说服力
_HALF_LIFE_DAYS = 14.0

DAY_WORKDAY = "workday"
DAY_WEEKEND = "weekend"
DAY_HOLIDAY = "holiday"

_DAY_NAMES = {
    DAY_WORKDAY: "工作日",
    DAY_WEEKEND: "周末",
    DAY_HOLIDAY: "节假日",
}


def bucket_of(when: dt.datetime) -> int:
    """把时间换算成 0~47 的时间桶。"""
    return (when.hour * 60 + when.minute) // BUCKET_MINUTES


def bucket_label(bucket: int) -> str:
    """时间桶渲染成 "07:30" 这样的起点时刻。"""
    minutes = bucket * BUCKET_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def day_kind_of(
    when: dt.datetime | dt.date,
    holidays: set[str] | None = None,
    workdays: set[str] | None = None,
) -> str:
    """判定某天属于工作日 / 周末 / 节假日（调休补班算工作日）。"""
    day = when.date() if isinstance(when, dt.datetime) else when
    iso = day.isoformat()
    if iso in (holidays or set()):
        return DAY_HOLIDAY
    if iso in (workdays or set()):
        return DAY_WORKDAY
    return DAY_WEEKEND if day.isoweekday() >= 6 else DAY_WORKDAY


def day_kind_label(kind: str) -> str:
    return _DAY_NAMES.get(kind, kind)


def opposite(direction: int) -> int:
    """1（上行）↔ 2（下行）。"""
    return 3 - direction


def _hhmm_to_minutes(token: str) -> int | None:
    """把 "07:30" / "0730" 解析成分钟数。"""
    text = (token or "").strip().replace("：", ":")
    if ":" in text:
        hour, _, minute = text.partition(":")
    elif text.isdigit() and len(text) in (3, 4):
        hour, minute = text[:-2], text[-2:]
    else:
        return None
    try:
        hour_value, minute_value = int(hour), int(minute)
    except ValueError:
        return None
    if not (0 <= hour_value < 24 and 0 <= minute_value < 60):
        return None
    return hour_value * 60 + minute_value


def parse_window(text: str, default: tuple[int, int]) -> tuple[int, int]:
    """解析 "07:00-09:00" 这样的时段配置，失败时用默认值。"""
    raw = (text or "").strip()
    for separator in ("-", "~", "至", "到", "—"):
        if separator in raw:
            start_text, _, end_text = raw.partition(separator)
            start = _hhmm_to_minutes(start_text)
            end = _hhmm_to_minutes(end_text)
            if start is not None and end is not None and start < end:
                return start, end
            break
    return default


def minutes_of(when: dt.datetime) -> int:
    return when.hour * 60 + when.minute


def in_window(minutes: int, window: tuple[int, int]) -> bool:
    start, end = window
    return start <= minutes < end


def _in_window(bucket: int, window: tuple[int, int]) -> bool:
    return in_window(bucket * BUCKET_MINUTES, window)


def _parse_dt(text: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


class ProfileStore:
    """出行画像的持久化与推断，JSON 文件存储（一个会话一份档案）。"""

    def __init__(self, data_dir: Path, retention_days: int = 30) -> None:
        self.path = Path(data_dir) / "profile.json"
        self.retention_days = max(1, int(retention_days or 30))
        self._profiles: dict[str, dict] = {}
        self.load()

    # ---------------- 读写 ----------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, ValueError) as exc:
            logger.error("读取出行画像失败：%s", exc)
            return

        profiles = payload.get("profiles") if isinstance(payload, dict) else None
        if isinstance(profiles, dict):
            self._profiles = {
                str(key): value
                for key, value in profiles.items()
                if isinstance(value, dict)
            }

    def save(self) -> None:
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
                json.dump(
                    {"profiles": self._profiles},
                    fp,
                    ensure_ascii=False,
                    indent=2,
                )
                tmp_path = fp.name
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logger.error("保存出行画像失败：%s", exc)

    def _profile(self, umo: str) -> dict:
        profile = self._profiles.get(str(umo))
        if profile is None:
            profile = {"places": {}, "observations": []}
            self._profiles[str(umo)] = profile
        profile.setdefault("places", {})
        profile.setdefault("observations", [])
        return profile

    # ---------------- 观测记录 ----------------

    def remember(
        self,
        umo: str,
        *,
        line: str,
        direction: int,
        station: str = "",
        city: str = "",
        source: str = "query",
        when: dt.datetime | None = None,
        weight: float = 1.0,
    ) -> None:
        """记一条出行观测。source 为 explicit（用户明说）/ inferred（插件推断）。"""
        if not line or direction not in (1, 2):
            return
        when = when or dt.datetime.now()
        profile = self._profile(umo)
        observations: list[dict] = profile["observations"]

        now = when
        for item in reversed(observations[-5:]):
            last = _parse_dt(item.get("when", ""))
            if last is None or (now - last).total_seconds() > _DEDUPE_MINUTES * 60:
                break
            if (
                item.get("line") == line
                and int(item.get("direction") or 0) == direction
                and (item.get("station") or "") == (station or "")
            ):
                item["when"] = now.isoformat(timespec="seconds")
                self.save()
                return

        observations.append(
            {
                "when": now.isoformat(timespec="seconds"),
                "bucket": bucket_of(now),
                "line": line,
                "direction": int(direction),
                "station": station or "",
                "city": city or "",
                "source": "explicit" if source == "explicit" else "inferred",
                "weight": float(weight),
            }
        )
        self._prune(profile, now)
        self.save()

    def _prune(self, profile: dict, now: dt.datetime) -> None:
        limit = now - dt.timedelta(days=self.retention_days)
        profile["observations"] = [
            item
            for item in profile.get("observations", [])
            if (_parse_dt(item.get("when", "")) or now) >= limit
        ]

    def observations(self, umo: str) -> list[dict]:
        return list(self._profile(umo).get("observations", []))

    # ---------------- 方向 / 站点推断 ----------------

    def predict(
        self,
        umo: str,
        when: dt.datetime,
        kind: str,
        *,
        line: str = "",
        station: str = "",
        min_samples: int = 2,
    ) -> dict | None:
        """按「同时段 + 同日类型」的历史观测推断方向与上车站。

        只用同一时间桶 ±1 的观测；时间越近权重越高。
        样本不足或没有候选项时返回 None。
        """
        observations = self._profile(umo).get("observations", [])
        if not observations:
            return None

        bucket = bucket_of(when)
        scores: dict[tuple[str, int], float] = {}
        stations: dict[tuple[str, int], dict[str, float]] = {}
        samples: dict[tuple[str, int], int] = {}
        hit = 0

        for item in observations:
            if day_kind_of(_parse_dt(item.get("when", "")) or when) != kind:
                continue
            distance = abs(int(item.get("bucket") or 0) - bucket)
            if distance > 1:
                continue
            if line and item.get("line") != line:
                continue
            if station and station not in (item.get("station") or ""):
                continue

            recorded = _parse_dt(item.get("when", "")) or when
            days_ago = max(0.0, (when - recorded).total_seconds() / 86400)
            decay = 0.5 ** (days_ago / _HALF_LIFE_DAYS)
            proximity = 1.0 if distance == 0 else 0.5
            score = float(item.get("weight") or 1.0) * decay * proximity

            key = (str(item.get("line") or ""), int(item.get("direction") or 0))
            scores[key] = scores.get(key, 0.0) + score
            samples[key] = samples.get(key, 0) + 1
            station_name = str(item.get("station") or "")
            if station_name:
                bucket_stations = stations.setdefault(key, {})
                bucket_stations[station_name] = (
                    bucket_stations.get(station_name, 0.0) + score
                )
            hit += 1

        if hit < max(1, min_samples) or not scores:
            return None

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0][1]))
        best_key, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        best_stations = stations.get(best_key) or {}
        best_station = (
            max(best_stations.items(), key=lambda kv: kv[1])[0] if best_stations else ""
        )

        return {
            "line": best_key[0],
            "direction": best_key[1],
            "station": best_station,
            "samples": samples.get(best_key, 0),
            "score": round(best_score, 2),
            "margin": round(best_score - second_score, 2),
        }

    # ---------------- 地点记忆 ----------------

    def places(self, umo: str) -> dict:
        """返回 {"home": {...} | None, "work": {...} | None, "frequent": [...]}"""
        stored = dict(self._profile(umo).get("places") or {})
        return {
            "home": stored.get("home"),
            "work": stored.get("work"),
            "frequent": list(stored.get("frequent") or []),
        }

    def find_place(self, umo: str, name: str) -> dict | None:
        """按地点名找一条地点记录（家 / 公司 / 常去都能命中）。"""
        token = (name or "").strip()
        if not token:
            return None
        stored = self._profile(umo).get("places") or {}
        for role in ("home", "work"):
            place = stored.get(role)
            if place and token == (place.get("name") or ""):
                return place
        for place in stored.get("frequent") or []:
            if token == (place.get("name") or ""):
                return place
        return None

    def set_place(
        self,
        umo: str,
        role: str,
        name: str,
        *,
        city: str = "",
        source: str = "explicit",
        when: dt.datetime | None = None,
    ) -> dict | None:
        """写入 / 更新一条地点记录。role 取 home / work / frequent。

        已存在的 explicit 记录不会被 inferred 覆盖。
        """
        token = (name or "").strip()
        if not token:
            return None
        profile = self._profile(umo)
        stored: dict = profile.setdefault("places", {})
        record = {
            "name": token,
            "city": city or "",
            "source": source,
            "updated": (when or dt.datetime.now()).date().isoformat(),
        }

        if role in ("home", "work"):
            current = stored.get(role)
            if current and current.get("source") == "explicit" and source != "explicit":
                return current
            stored[role] = record
            self._drop_frequent(stored, token)
            self.save()
            return record

        if role == "frequent":
            if stored.get("home", {}).get("name") == token:
                return stored["home"]
            if stored.get("work", {}).get("name") == token:
                return stored["work"]
            frequent = stored.setdefault("frequent", [])
            for index, item in enumerate(frequent):
                if item.get("name") == token:
                    frequent[index] = {**item, **record}
                    self.save()
                    return frequent[index]
            frequent.append(record)
            del frequent[:-8]
            self.save()
            return record
        return None

    def remove_place(self, umo: str, role: str) -> bool:
        stored = self._profile(umo).get("places") or {}
        if role == "frequent":
            if not stored.get("frequent"):
                return False
            stored["frequent"] = []
            self.save()
            return True
        if role in ("home", "work") and stored.get(role):
            del stored[role]
            self.save()
            return True
        return False

    @staticmethod
    def _drop_frequent(stored: dict, name: str) -> None:
        frequent = stored.get("frequent")
        if isinstance(frequent, list):
            stored["frequent"] = [item for item in frequent if item.get("name") != name]

    def learn_places(self, umo: str, *, min_hits: int = 2) -> dict:
        """从历史观测里推断家和公司：早高峰出发站 ↔ 晚高峰出发站（方向相反）。

        只填空缺，不会覆盖用户明说的地点。返回本次新学到的东西。
        """
        observations = self._profile(umo).get("observations", [])
        morning: dict[tuple[str, int], int] = {}
        evening: dict[tuple[str, int], int] = {}

        for item in observations:
            if day_kind_of(_parse_dt(item.get("when", "")) or dt.datetime.now()) != (
                DAY_WORKDAY
            ):
                continue
            station = str(item.get("station") or "")
            if not station:
                continue
            bucket = int(item.get("bucket") or 0)
            direction = int(item.get("direction") or 0)
            target = None
            if _in_window(bucket, MORNING_WINDOW):
                target = morning
            elif _in_window(bucket, EVENING_WINDOW):
                target = evening
            if target is None:
                continue
            key = (station, direction)
            target[key] = target.get(key, 0) + 1

        learned: dict[str, str] = {}
        if morning and evening:
            home_key = max(morning.items(), key=lambda kv: kv[1])[0]
            work_candidates = [
                (key, hits)
                for key, hits in evening.items()
                if key[1] == opposite(home_key[1]) and hits >= min_hits
            ]
            if morning[home_key] >= min_hits and work_candidates:
                work_key = max(work_candidates, key=lambda kv: kv[1])[0]
                stored = self._profile(umo).get("places") or {}
                if not (stored.get("home") or {}).get("name"):
                    record = self.set_place(umo, "home", home_key[0])
                    if record:
                        learned["home"] = record["name"]
                if not (stored.get("work") or {}).get("name"):
                    record = self.set_place(umo, "work", work_key[0])
                    if record:
                        learned["work"] = record["name"]

        # 常去：出现次数够多、又不是家 / 公司的候车站
        station_hits: dict[str, int] = {}
        for item in observations:
            station = str(item.get("station") or "")
            if station:
                station_hits[station] = station_hits.get(station, 0) + 1
        stored = self._profile(umo).get("places") or {}
        known = {
            (stored.get("home") or {}).get("name"),
            (stored.get("work") or {}).get("name"),
        }
        threshold = max(3, min_hits + 1)
        hot = sorted(
            (
                (name, hits)
                for name, hits in station_hits.items()
                if name not in known and hits >= threshold
            ),
            key=lambda kv: -kv[1],
        )[:5]
        for name, _hits in hot:
            self.set_place(umo, "frequent", name)
        return learned

    # ---------------- 展示 / 清理 ----------------

    def describe(self, umo: str, *, top: int = 4) -> str:
        """把画像整理成 /bushabit 用的文本。"""
        profile = self._profile(umo)
        observations = profile.get("observations", [])
        places = profile.get("places") or {}
        lines: list[str] = []

        home = places.get("home")
        work = places.get("work")
        frequent = places.get("frequent") or []
        if home or work or frequent:
            lines.append("🏠 地点记忆")
            if home:
                lines.append(f"  · 家：{home.get('name')}（{home.get('source')}）")
            if work:
                lines.append(f"  · 公司：{work.get('name')}（{work.get('source')}）")
            if frequent:
                names = "、".join(str(item.get("name")) for item in frequent)
                lines.append(f"  · 常去：{names}")

        if not observations:
            lines.append(
                "\n📚 还没有出行记录。查几次「XX路到哪了」之后，我就能记住你的通勤习惯了。"
            )
            return "\n".join(lines)

        # 按 (日类型, 时间桶) 聚合，取频次最高的几组
        grouped: dict[tuple[str, int], dict[tuple[str, int, str], int]] = {}
        for item in observations:
            when = _parse_dt(item.get("when", ""))
            if when is None:
                continue
            kind = day_kind_of(when)
            key = (kind, int(item.get("bucket") or 0))
            entry = grouped.setdefault(key, {})
            detail = (
                str(item.get("line") or ""),
                int(item.get("direction") or 0),
                str(item.get("station") or ""),
            )
            entry[detail] = entry.get(detail, 0) + 1

        lines.append(f"\n📚 出行习惯（共 {len(observations)} 条记录，保留最近若干天）")
        ranked = sorted(
            grouped.items(),
            key=lambda kv: -sum(kv[1].values()),
        )[:top]
        for (kind, bucket), entry in ranked:
            detail, hits = max(entry.items(), key=lambda kv: kv[1])
            line, direction, station = detail
            text = f"  · {day_kind_label(kind)} {bucket_label(bucket)} 前后"
            text += f"：{line} 方向{direction}"
            if station:
                text += f" · 候车 {station}"
            text += f"（{hits} 次）"
            lines.append(text)
        return "\n".join(lines)

    def clear(
        self, umo: str, *, places: bool = True, observations: bool = True
    ) -> None:
        profile = self._profile(umo)
        if observations:
            profile["observations"] = []
        if places:
            profile["places"] = {}
        self._prune(profile, dt.datetime.now())
        self.save()

    def stats(self, umo: str) -> dict:
        profile = self._profile(umo)
        places = profile.get("places") or {}
        return {
            "observations": len(profile.get("observations") or []),
            "home": bool(places.get("home")),
            "work": bool(places.get("work")),
            "frequent": len(places.get("frequent") or []),
        }
