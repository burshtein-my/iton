"""Отбор важного и написание заметок через Claude API."""
from __future__ import annotations

import json
import os
import re

import anthropic

from .collect import Item, fetch_text

READER = (
    "Читательница — Мария, фрилансер из Израиля: графический дизайн, вёрстка, веб-разработка, SMM, "
    "контент-маркетинг. Пользуется Claude и Claude Code. Хочет первой узнавать о НОВЫХ ФУНКЦИОНАЛЬНЫХ "
    "ВОЗМОЖНОСТЯХ нейросетей крупнейших американских и китайских компаний и о стоящих расширениях для "
    "Claude и Claude Code, которые популярны у гиков."
)

RULES = """БРАТЬ:
- новые модели и версии; новые функции в продуктах (ChatGPT, Claude, Gemini, Grok, Qwen, Kimi и т.д.);
- изменения API, цен, лимитов, бесплатных тарифов; открытые веса;
- доступность в новых странах и языках (особенно Израиль, иврит, русский);
- агенты, интеграции, генерация изображений, видео, звука, инструменты для кода, веба, дизайна, маркетинга;
- для Claude и Claude Code — ФИШКИ: skills, MCP-серверы, плагины, надстройки, приёмы, которые можно применить сегодня,
  особенно для дизайна, видеомонтажа, продвижения в интернете и жизненных задач; новые версии Claude Code — только
  с новыми возможностями. Брать, если это популярно (закладки, рейтинг, очки, звёзды), официально или от любимого автора.
  Статьи «любимого автора» берутся, если тема хоть немного подходит под вкус читательницы.
НЕ БРАТЬ:
- инвестиции, оценки компаний, IPO, кадровые новости, суды, политику, мнения, прогнозы;
- слухи и утечки; бенчмарки без реальной доступности; партнёрства без новой функции;
- рекламные статьи компаний, пересказы документации, подборки «10 промптов»;
- повторы одной и той же новости — из дублей выбери лучший источник (первоисточник важнее СМИ)."""

CHOOSE_TOOL = {
    "name": "choose",
    "description": "Список отобранных публикаций для утреннего номера.",
    "input_schema": {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ids": {"type": "array", "items": {"type": "integer"},
                                "description": "Номера публикаций об одном и том же; первым — лучший источник"},
                        "section": {"type": "string", "enum": ["us", "china", "claude"]},
                        "priority": {"type": "integer", "enum": [1, 2, 3],
                                     "description": "1 — главная новость дня, 3 — второстепенная"},
                        "reason": {"type": "string", "description": "Коротко: что нового и почему это важно"},
                    },
                    "required": ["ids", "section", "priority", "reason"],
                },
            }
        },
        "required": ["picks"],
    },
}

ARTICLE = {
    "type": "object",
    "properties": {
        "section": {"type": "string", "enum": ["us", "china", "claude"]},
        "is_lead": {"type": "boolean", "description": "true только у одной, самой важной новости номера"},
        "kicker": {"type": "string", "description": "Надзаголовок: компания или тема, 1–3 слова"},
        "title": {"type": "string", "description": "Заголовок по-русски, до 12 слов, по сути, без восторгов"},
        "text": {"type": "string", "description": "Заметка по-русски. Главная — 90–140 слов, остальные — 40–80 слов. "
                                                  "Что вышло и что теперь конкретно можно сделать."},
        "availability": {"type": "string", "description": "Где и почём доступно: бесплатно/платно/API/открытые веса/регионы. "
                                                          "Пусто, если в материалах этого нет"},
        "popularity": {"type": "string", "description": "Для блока Claude: рейтинг, закладки, очки, звёзды — как в материалах"},
        "install": {"type": "string", "description": "Для расширений: как поставить, одной строкой, если это просто; иначе пусто"},
        "warning": {"type": "boolean", "description": "true, если расширение от малоизвестного автора и просит широкий доступ "
                                                     "(файлы, почта, ключи)"},
        "source_name": {"type": "string"},
        "url": {"type": "string", "description": "Ссылка — строго одна из ссылок, данных в материалах"},
    },
    "required": ["section", "is_lead", "kicker", "title", "text", "source_name", "url"],
}

ISSUE_TOOL = {
    "name": "issue",
    "description": "Готовый номер газеты.",
    "input_schema": {
        "type": "object",
        "properties": {
            "top": {"type": "array", "items": {"type": "string"},
                    "description": "«Главное»: 1–3 пункта, каждый одной строкой до 15 слов"},
            "articles": {"type": "array", "items": ARTICLE},
            "try_this": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "why": {"type": "string"}, "url": {"type": "string"}},
                    "required": ["title", "why", "url"],
                },
                "description": "«Стоит попробовать»: 0–2 пункта, полезные для дизайна, веб-разработки или SMM",
            },
        },
        "required": ["top", "articles", "try_this"],
    },
}


def _client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("Не задан секрет ANTHROPIC_API_KEY")
    return anthropic.Anthropic(max_retries=4)


_AUTO_ONLY: set[str] = set()   # модели, которые не принимают обязательный вызов инструмента


def _call_tool(client, model: str, system: str, user: str, tool: dict, max_tokens: int) -> tuple[dict, dict]:
    """Просим модель вернуть результат через инструмент; если модель так не умеет — мягкий режим."""
    kwargs = dict(model=model, max_tokens=max_tokens, tools=[tool],
                  messages=[{"role": "user", "content": user}])
    soft_system = (system + f"\n\nОтвет дай одним вызовом инструмента {tool['name']}, "
                            "без текста до и после.")
    resp = None
    if model not in _AUTO_ONLY:
        try:
            resp = client.messages.create(system=system, tool_choice={"type": "tool", "name": tool["name"]}, **kwargs)
        except Exception as exc:  # некоторые модели (с постоянным «размышлением») не принимают type=tool
            if "tool_choice" not in str(exc):
                raise
            _AUTO_ONLY.add(model)
    if resp is None:
        resp = client.messages.create(system=soft_system, tool_choice={"type": "auto"}, **kwargs)

    usage = {"model": model, "input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    for block in resp.content:
        if block.type == "tool_use" and getattr(block, "name", tool["name"]) == tool["name"]:
            return block.input, usage
    # запасной путь: модель ответила JSON-текстом вместо вызова инструмента
    text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0)), usage
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"Модель {model} не вернула результат (stop_reason: {getattr(resp, 'stop_reason', '?')})")


def _reader(config: dict) -> str:
    taste = (config.get("taste") or "").strip()
    return READER + (f"\n\nЕё вкус, своими словами:\n{taste}" if taste else "")


def select(items: list[Item], config: dict) -> tuple[list[dict], dict]:
    """Шаг 1: дешёвая модель выбирает важное из общего списка."""
    lines = []
    for i, it in enumerate(items):
        lines.append(
            f"[{i}] ({it.kind}, {it.group}) {it.source} | {it.date_str()} | {it.title}"
            f" | {it.summary[:300]}" + (f" | {it.metrics}" if it.metrics else "")
        )
    max_n = config["limits"]["max_selected"]
    system = (f"Ты — выпускающий редактор утренней газеты о нейросетях. {_reader(config)}\n\n{RULES}\n\n"
              f"Выбери не больше {max_n} публикаций. Лучше меньше, но только действительно новое и полезное. "
              "Если ничего стоящего нет — верни пустой список.")
    user = "Свежие публикации за последние сутки (для сообществ — за несколько дней):\n\n" + "\n".join(lines)
    data, usage = _call_tool(_client(), config["models"]["select"], system, user, CHOOSE_TOOL, 8000)
    picks = []
    for p in data.get("picks", [])[:max_n]:
        ids = [i for i in p.get("ids", []) if isinstance(i, int) and 0 <= i < len(items)]
        if ids:
            picks.append({**p, "ids": ids})
    return picks, usage


def write(items: list[Item], picks: list[dict], config: dict, issue_date: str) -> tuple[dict, dict]:
    """Шаг 2: сильная модель читает отобранное и пишет номер по-русски."""
    limit = config["limits"]["article_chars"]
    blocks, allowed_urls = [], set()
    for n, p in enumerate(picks, 1):
        main = items[p["ids"][0]]
        best = next((items[i] for i in p["ids"] if items[i].fetchable), main)
        text = fetch_text(best, limit) or main.summary
        urls = [items[i].url for i in p["ids"]]
        allowed_urls.update(urls)
        blocks.append(
            f"### Материал {n} (раздел: {p['section']}, важность {p['priority']})\n"
            f"Заголовок: {main.title}\nИсточник: {main.source}\nДата: {main.date_str()}\n"
            f"Ссылки: {' ; '.join(urls)}\n"
            + (f"Популярность: {main.metrics}\n" if main.metrics else "")
            + f"Почему отобрано: {p['reason']}\n\nТекст:\n{text}\n"
        )
    system = (
        f"Ты пишешь утренний номер газеты о нейросетях. {_reader(config)}\n\n"
        "Правила:\n"
        "- Пиши по-русски, прямо и по делу, без восторгов и маркетинговых слов. Названия моделей, продуктов, "
        "репозиториев и компаний — в оригинале.\n"
        "- Используй ТОЛЬКО факты из материалов. Нет цены или условий — так и не пиши, оставь поле пустым. "
        "Ничего не додумывай.\n"
        "- Одна заметка на материал. Если материал оказался пустым, рекламным или неважным — пропусти его.\n"
        "- Ровно одна заметка с is_lead=true — самая важная новость дня.\n"
        "- Ссылка в каждой заметке — одна из ссылок этого материала, предпочтительно первоисточник.\n"
        "- Раздел us — США, china — Китай, claude — фишки, расширения и новости для Claude и Claude Code.\n"
        "- В заметках раздела claude объясняй, как применить фишку в задачах читательницы (дизайн, видео, "
        "продвижение, бытовые дела), если материал это позволяет. Не выдумывай применение, которого нет в тексте."
    )
    user = f"Дата номера: {issue_date}.\n\nМатериалы:\n\n" + "\n\n".join(blocks)
    data, usage = _call_tool(_client(), config["models"]["write"], system, user, ISSUE_TOOL, 16000)

    # страховка: ссылки только из материалов, ровно одна главная
    for a in data.get("articles", []):
        if a.get("url") not in allowed_urls:
            a["url"] = ""
    articles = data.get("articles", [])
    leads = [a for a in articles if a.get("is_lead")]
    if articles and not leads:
        articles[0]["is_lead"] = True
    for extra in leads[1:]:
        extra["is_lead"] = False
    data["try_this"] = [t for t in data.get("try_this", [])[:2] if t.get("url") in allowed_urls or not t.get("url")]
    return data, usage


def cost_usd(usages: list[dict]) -> float:
    """Примерная стоимость по прайсу на ноябрь 2026 (USD за миллион токенов)."""
    prices = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
    total = 0.0
    for u in usages:
        p_in, p_out = prices.get(u["model"], (2.0, 10.0))
        total += u["input"] / 1e6 * p_in + u["output"] / 1e6 * p_out
    return total
