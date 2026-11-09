"""Память между запусками: номер выпуска, что уже было в газете, известные ссылки на страницах."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from .util import now_utc

KEEP_DAYS = 45


class State:
    def __init__(self, path: Path):
        self.path = path
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        self.issue: int = int(data.get("issue", 0))
        self.seen: dict[str, str] = dict(data.get("seen", {}))      # url_key -> дата публикации в газете
        self.pages: dict[str, list[str]] = dict(data.get("pages", {}))  # имя страницы -> известные ссылки

    def is_seen(self, key: str) -> bool:
        return key in self.seen

    def mark_seen(self, keys) -> None:
        today = now_utc().date().isoformat()
        for k in keys:
            self.seen[k] = today

    def prune(self) -> None:
        border = (now_utc() - timedelta(days=KEEP_DAYS)).date().isoformat()
        self.seen = {k: d for k, d in self.seen.items() if d >= border}
        for name, links in self.pages.items():
            self.pages[name] = links[-400:]

    def save(self) -> None:
        self.prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"issue": self.issue, "seen": self.seen, "pages": self.pages}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
