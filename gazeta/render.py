"""Вёрстка газеты (PDF) и письма (HTML и текст)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .util import domain

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]
SECTION_NAMES = {"us": "США", "china": "Китай", "claude": "Claude и Claude Code"}
SECTION_ORDER = ("us", "china", "claude")


def ru_date(dt: datetime) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def ru_weekday_date(dt: datetime) -> str:
    return f"{WEEKDAYS[dt.weekday()].capitalize()}, {ru_date(dt)}"


def _env(root: Path) -> Environment:
    env = Environment(loader=FileSystemLoader(str(root / "templates")),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["domain"] = domain
    return env


def context(issue: dict, config: dict, number: int, today: datetime, stats: dict) -> dict:
    arts = [a for a in issue.get("articles", []) if a.get("title") and a.get("text")]
    lead = next((a for a in arts if a.get("is_lead")), None)
    sections = []
    for key in SECTION_ORDER:
        group = [a for a in arts if a.get("section") == key and a is not lead]
        if group:
            sections.append({"key": key, "name": SECTION_NAMES[key], "articles": group})
    paper = config["newspaper"]
    return {
        "title": paper["title"], "subtitle": paper.get("subtitle", ""), "city": paper.get("city", ""),
        "issue": number, "weekday_date": ru_weekday_date(today), "short_date": ru_date(today),
        "top": issue.get("top", [])[:3], "lead": lead, "sections": sections,
        "try_this": issue.get("try_this", [])[:2], "section_names": SECTION_NAMES,
        "n_items": stats.get("n_items", 0), "n_sources": stats.get("n_sources", 0),
        "n_articles": len(arts), "failed": stats.get("failed", []), "demo": stats.get("demo", False),
    }


def render_pdf(ctx: dict, out_dir: Path, root: Path, today: datetime) -> Path:
    from weasyprint import HTML  # тяжёлый импорт — только когда нужен

    html = _env(root).get_template("newspaper.html").render(**ctx)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "gazeta.html").write_text(html, encoding="utf-8")
    pdf_path = out_dir / f"ai-gazeta-{today:%Y-%m-%d}.pdf"
    HTML(string=html, base_url=str(root)).write_pdf(str(pdf_path))
    return pdf_path


def render_email(ctx: dict, root: Path) -> tuple[str, str]:
    html = _env(root).get_template("email.html").render(**ctx)
    lines = [f"{ctx['title']} · № {ctx['issue']} · {ctx['weekday_date']}", ""]
    if ctx["top"]:
        lines.append("ГЛАВНОЕ")
        lines += [f"{i}. {t}" for i, t in enumerate(ctx["top"], 1)]
        lines.append("")
    blocks = ([("Главная новость", [ctx["lead"]])] if ctx["lead"] else []) + \
             [(s["name"], s["articles"]) for s in ctx["sections"]]
    for name, arts in blocks:
        lines.append(name.upper())
        for a in arts:
            lines.append(f"— {a['title']}")
            lines.append(a["text"])
            for label, key in (("Доступ", "availability"), ("Популярность", "popularity"), ("Установка", "install")):
                if a.get(key):
                    lines.append(f"{label}: {a[key]}")
            if a.get("warning"):
                lines.append("Внимание: проверить перед установкой.")
            lines.append(f"{a.get('source_name', '')} {a.get('url', '')}".strip())
            lines.append("")
    if ctx["try_this"]:
        lines.append("СТОИТ ПОПРОБОВАТЬ")
        for t in ctx["try_this"]:
            lines.append(f"— {t['title']}: {t['why']} {t.get('url', '')}".strip())
    return html, "\n".join(lines)
