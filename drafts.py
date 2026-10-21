"""待确认操作草稿。

AI 只能把订阅变更整理成草稿，真正的落库要等用户回复确认码。
草稿只存在内存里，绑定「会话 + 操作者」，默认 5 分钟过期，重载插件后自动失效。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TTL_SECONDS = 300
# 去掉容易看错的 I / O / 0 / 1
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass
class Draft:
    """一条等待用户确认的变更。"""

    code: str
    action: str
    payload: dict[str, Any]
    preview: str
    umo: str
    sender_id: str
    expires_at: float
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


class DraftStore:
    """内存态草稿表。确认码一次性使用，按会话与操作者绑定。"""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = max(30, int(ttl_seconds))
        self._items: dict[str, Draft] = {}

    def create(
        self,
        *,
        action: str,
        payload: dict[str, Any],
        preview: str,
        umo: str,
        sender_id: str,
    ) -> Draft:
        """新建草稿并返回带确认码的对象。"""
        self.purge()
        code = self._new_code()
        draft = Draft(
            code=code,
            action=action,
            payload=dict(payload),
            preview=preview,
            umo=umo,
            sender_id=sender_id or "",
            expires_at=time.time() + self.ttl,
        )
        self._items[code] = draft
        return draft

    def pop(self, code: str, *, umo: str, sender_id: str) -> Draft | None:
        """取出并作废草稿；会话或操作者不匹配时返回 None。"""
        self.purge()
        token = (code or "").strip().upper()
        draft = self._items.get(token)
        if draft is None:
            return None
        if draft.umo != umo or draft.sender_id != (sender_id or ""):
            return None
        return self._items.pop(token, None)

    def purge(self) -> None:
        """清掉过期草稿。"""
        now = time.time()
        for token in [key for key, item in self._items.items() if item.expires_at <= now]:
            self._items.pop(token, None)

    def _new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
            if code not in self._items:
                return code
