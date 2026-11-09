"""Сбор свежих публикаций из всех источников, перечисленных в config.yaml."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

import feedparser

from .state import State
from .util import http_get, now_utc, parse_iso, shorten, strip_html, url_key

HABR_PAUSE = 2.0  # секунд между запросами к Хабру


@dataclass
class Item:
    source: str
    group: str                 # us | china | claude | other
    kind: str                  # company | community
    title: str
    url: str
    published: datetime | None = None
    summary: str = ""
    metrics: str = ""
    fetchable: bool = True     # можно ли скачать полный текст по ссылке
    extra_text: str = ""       # текст, который уже есть без скачивания (заметки к релизу и т.п.)

    @property
    def key(self) -> str:
        return url_key(self.url)

    def date_str(self) -> str:
        return self.published.strftime("%Y-%m-%d %H:%M") if self.published else "дата неизвестна"


@dataclass
class Collector:
    config: dict
    state: State
    items: list[Item] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)       # источники компаний, которые не ответили
    log: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.now = now_utc()
        lim = self.config["limits"]
        self.company_cut = self.now - timedelta(hours=lim["company_window_hours"])
        self.community_cut = self.now - timedelta(hours=lim["community_window_hours"])
        self.pop = self.config["popularity"]
        self.habr_cfg = self.config.get("habr") or {}
        self.habr_favorites = {a.lower() for a in self.habr_cfg.get("favorite_authors", [])}
        self.habr_blocked = {a.lower() for a in self.habr_cfg.get("blocked_authors", [])}

    # ---------- общий запуск ----------
    def run(self) -> list[Item]:
        src = self.config["sources"]
        for s in src.get("rss", []):
            self._safe(f"RSS {s['name']}", self.rss, s, company=True)
        for s in src.get("pages", []):
            self._safe(f"Сайт {s['name']}", self.page, s, company=True)
        for s in src.get("google_news", []):
            self._safe(f"Google News «{s['q']}»", self.google_news, s, company=True)
        for group, authors in (src.get("huggingface_authors") or {}).items():
            for author in authors:
                self._safe(f"Hugging Face {author}", self.huggingface, author, group, company=True)
        for login in self.habr_cfg.get("favorite_authors", []):
            self._safe(f"Хабр, автор {login}", self.habr_author, login, company=False)
        for q in self.habr_cfg.get("queries", []):
            self._safe(f"Хабр «{q}»", self.habr, q, company=False)
        for q in src.get("hn_queries", []):
            self._safe(f"Hacker News «{q}»", self.hacker_news, q, company=False)
        for sub in src.get("reddit", []):
            self._safe(f"Reddit r/{sub}", self.reddit, sub, company=False)
        for q in src.get("github_queries", []):
            self._safe(f"GitHub «{q}»", self.github, q, company=False)
        return self._finish()

    def _safe(self, label, fn, *args, company: bool):
        try:
            n = fn(*args)
            self.log.append(f"ok    {label}: {n}")
        except Exception as exc:  # источник не ответил — идём дальше
            self.log.append(f"FAIL  {label}: {type(exc).__name__}: {shorten(str(exc), 160)}")
            if company:
                self.failed.append(label)

    def _add(self, item: Item) -> bool:
        cut = self.company_cut if item.kind == "company" else self.community_cut
        if item.published and item.published < cut:
            return False
        if not item.title or not item.url:
            return False
        self.items.append(item)
        return True

    def _finish(self) -> list[Item]:
        unique: dict[str, Item] = {}
        titles: set[str] = set()
        for it in sorted(self.items, key=lambda i: i.published or self.now, reverse=True):
            if self.state.is_seen(it.key) or it.key in unique:
                continue
            tkey = re.sub(r"\W+", " ", it.title.lower()).strip()
            if tkey in titles:
                continue
            titles.add(tkey)
            unique[it.key] = it
        self.items = list(unique.values())
        self.log.append(f"Итого новых публикаций: {len(self.items)}")
        return self.items

    # ---------- источники ----------
    def rss(self, s: dict) -> int:
        resp = http_get(s["url"])
        feed = feedparser.parse(resp.content)
        n = 0
        for e in feed.entries[:30]:
            published = _feed_date(e)
            summary = strip_html(e.get("summary", ""))
            extra = ""
            if e.get("content"):
                extra = strip_html(e["content"][0].get("value", ""))
            item = Item(
                source=s["name"], group=s.get("group", "other"), kind="company",
                title=strip_html(e.get("title", "")), url=e.get("link", ""),
                published=published, summary=shorten(summary, 500),
                extra_text=shorten(extra, 3000) if s.get("group") == "claude" else "",
            )
            n += self._add(item)
        return n

    def page(self, s: dict) -> int:
        """Страница без RSS: при первом запуске запоминаем ссылки, дальше сообщаем о новых."""
        html_text = http_get(s["url"]).text
        pattern = re.compile(s["pattern"])
        found: dict[str, str] = {}
        for m in re.finditer(r'<a\b[^>]*?href="([^"#?]+)"[^>]*>(.*?)</a>', html_text, re.S | re.I):
            href, inner = m.group(1), m.group(2)
            if not pattern.search(href):
                continue
            link = urljoin(s["url"], href)
            if url_key(link) == url_key(s["url"]):
                continue
            title = strip_html(inner)
            if link not in found or (title and len(title) > len(found[link])):
                found[link] = title
        if not found:
            raise RuntimeError("на странице не найдено ни одной ссылки — возможно, сайт поменял вёрстку")
        known = self.state.pages.get(s["name"])
        self.state.pages[s["name"]] = list(dict.fromkeys((known or []) + list(found)))
        if known is None:            # первый запуск: только запоминаем
            return 0
        n = 0
        for link, title in found.items():
            if link in known:
                continue
            if not title:
                title = link.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").capitalize()
            n += self._add(Item(source=s["name"], group=s.get("group", "other"), kind="company",
                                title=shorten(title, 200), url=link, published=None))
        return n

    def google_news(self, s: dict) -> int:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(f"{s['q']} when:1d")
               + "&hl=en-US&gl=US&ceid=US:en")
        feed = feedparser.parse(http_get(url).content)
        n = 0
        for e in feed.entries[:8]:
            outlet = (e.get("source") or {}).get("title", "")
            title = strip_html(e.get("title", ""))
            if outlet and title.endswith(" - " + outlet):
                title = title[: -len(outlet) - 3]
            item = Item(
                source=f"Google News · {outlet or 'СМИ'}", group=s.get("group", "other"), kind="company",
                title=title, url=e.get("link", ""), published=_feed_date(e),
                summary=shorten(strip_html(e.get("summary", "")), 300), fetchable=False,
            )
            n += self._add(item)
        return n

    def huggingface(self, author: str, group: str) -> int:
        data = http_get("https://huggingface.co/api/models",
                        params={"author": author, "sort": "createdAt", "direction": "-1", "limit": "8"}).json()
        n = 0
        for m in data:
            mid = m.get("id") or m.get("modelId")
            if not mid:
                continue
            tags = [t for t in m.get("tags", []) if not t.startswith(("region:", "endpoints", "autotrain"))][:8]
            item = Item(
                source="Hugging Face", group=group, kind="company",
                title=f"Новая модель на Hugging Face: {mid}",
                url=f"https://huggingface.co/{mid}", published=parse_iso(m.get("createdAt")),
                summary=f"Тип: {m.get('pipeline_tag') or '—'}; теги: {', '.join(tags)}",
                metrics=f"лайков {m.get('likes', 0)}, загрузок {m.get('downloads', 0)}",
            )
            n += self._add(item)
        return n

    def habr(self, query: str) -> int:
        """Поиск по Хабру: берём только то, что гики уже оценили (закладки важнее рейтинга)."""
        n = 0
        for page in (1, 2):
            time.sleep(HABR_PAUSE)  # Хабр ограничивает частые запросы
            data = http_get("https://habr.com/kek/v2/articles/", params={
                "query": query, "order": "date", "fl": "ru", "hl": "ru", "page": str(page)}).json()
            refs = data.get("publicationRefs") or {}
            for art in refs.values():
                author = (art.get("author") or {}).get("alias", "")
                if author.lower() in self.habr_blocked:
                    continue
                st = art.get("statistics") or {}
                score, fav, com = st.get("score", 0), st.get("favoritesCount", 0), st.get("commentsCount", 0)
                plus, minus = st.get("votesCountPlus", 0), st.get("votesCountMinus", 0)
                corp = [h.get("title", "") for h in art.get("hubs", []) if h.get("type") == "corporative"]
                favorite = author.lower() in self.habr_favorites
                k = 1 if favorite or not (corp or art.get("isCorporative")) else self.habr_cfg.get("corporate_factor", 2)
                popular = (score >= self.pop["habr_min_score"] * k or fav >= self.pop["habr_min_favorites"] * k
                           or com >= self.pop["habr_min_comments"] * k)
                if plus + minus >= 5 and minus / (plus + minus) > self.habr_cfg.get("max_minus_share", 0.35):
                    popular = False
                if not (popular or favorite):
                    continue
                n += self._add(self._habr_item(art, author, favorite, corp,
                                               f"рейтинг {score:+d}, в закладках {fav}, комментариев {com}"))
        return n

    def habr_author(self, login: str) -> int:
        """Лента любимого автора: каждая новая статья идёт в отбор без порога популярности."""
        time.sleep(HABR_PAUSE)
        feed = feedparser.parse(http_get(f"https://habr.com/ru/rss/users/{login}/publications/articles/",
                                         params={"fl": "ru"}).content)
        n = 0
        for e in feed.entries[:10]:
            item = Item(
                source="Хабр", group="claude", kind="community",
                title=strip_html(e.get("title", "")), url=e.get("link", ""), published=_feed_date(e),
                summary=f"Автор: {login} (любимый автор). " + shorten(strip_html(e.get("summary", "")), 350),
                metrics="любимый автор",
            )
            n += self._add(item)
        return n

    def _habr_item(self, art: dict, author: str, favorite: bool, corp: list, metrics: str) -> Item:
        full = (art.get("author") or {}).get("fullname") or ""
        tags = ", ".join(t.get("titleHtml", "") for t in (art.get("tags") or [])[:8] if isinstance(t, dict))
        who = f"Автор: {author}" + (f" ({full})" if full else "") + (", любимый автор" if favorite else "")
        if corp:
            who += f"; {corp[0]}"
        lead = strip_html((art.get("leadData") or {}).get("textHtml", ""))
        summary = f"{who}. " + (f"Теги: {tags}. " if tags else "") + lead
        return Item(
            source="Хабр", group="claude", kind="community",
            title=strip_html(art.get("titleHtml", "")), url=f"https://habr.com/ru/articles/{art['id']}/",
            published=parse_iso(art.get("timePublished")), summary=shorten(summary, 450),
            metrics=metrics + (", любимый автор" if favorite else ""),
        )

    def hacker_news(self, query: str) -> int:
        since = int(self.community_cut.timestamp())
        data = http_get("https://hn.algolia.com/api/v1/search", params={
            "query": query, "tags": "story", "hitsPerPage": "20",
            "numericFilters": f"created_at_i>{since},points>={self.pop['hn_min_points']}"}).json()
        n = 0
        for h in data.get("hits", []):
            discussion = f"https://news.ycombinator.com/item?id={h['objectID']}"
            url = h.get("url") or discussion
            item = Item(
                source="Hacker News", group="other", kind="community",
                title=h.get("title", ""), url=url, published=parse_iso(h.get("created_at")),
                summary=f"Обсуждение: {discussion}",
                metrics=f"{h.get('points', 0)} очков, {h.get('num_comments', 0)} комментариев на HN",
                fetchable=url != discussion,
            )
            n += self._add(item)
        return n

    def reddit(self, sub: str) -> int:
        data = http_get(f"https://www.reddit.com/r/{sub}/top.json", params={"t": "day", "limit": "25"}).json()
        n = 0
        for child in data.get("data", {}).get("children", []):
            p = child.get("data", {})
            if p.get("ups", 0) < self.pop["reddit_min_upvotes"]:
                continue
            permalink = "https://www.reddit.com" + p.get("permalink", "")
            item = Item(
                source=f"Reddit r/{sub}", group="other", kind="community",
                title=p.get("title", ""), url=permalink,
                published=datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc),
                summary=shorten(p.get("selftext", ""), 400),
                metrics=f"{p.get('ups', 0)} апвоутов, {p.get('num_comments', 0)} комментариев",
                fetchable=False, extra_text=shorten(p.get("selftext", ""), 3000),
            )
            n += self._add(item)
        return n

    def github(self, query: str) -> int:
        since = self.community_cut.date().isoformat()
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = http_get("https://api.github.com/search/repositories", headers=headers, params={
            "q": f"{query} created:>{since}", "sort": "stars", "order": "desc", "per_page": "10"}).json()
        n = 0
        for r in data.get("items", []):
            stars = r.get("stargazers_count", 0)
            if stars < self.pop["github_min_stars"]:
                continue
            owner = (r.get("owner") or {}).get("login", "")
            item = Item(
                source="GitHub", group="claude", kind="community",
                title=f"{r.get('full_name')}: {r.get('description') or 'без описания'}",
                url=r.get("html_url", ""), published=parse_iso(r.get("created_at")),
                summary=f"Автор: {owner}; язык: {r.get('language') or '—'}; темы: {', '.join(r.get('topics', [])[:8])}",
                metrics=f"звёзд на GitHub: {stars} с {r.get('created_at', '')[:10]}",
            )
            n += self._add(item)
        return n


def _feed_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_text(item: Item, limit: int) -> str:
    """Полный текст публикации для Claude (с ограничением по длине)."""
    parts = []
    if item.extra_text:
        parts.append(item.extra_text)
    if item.fetchable:
        try:
            import trafilatura
            html_text = http_get(item.url, timeout=20).text
            text = trafilatura.extract(html_text, include_comments=False, favor_recall=True) or ""
            if text:
                parts.append(text)
        except Exception:
            pass
    if not parts and item.summary:
        parts.append(item.summary)
    return shorten("\n\n".join(parts), limit)
