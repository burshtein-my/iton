"""AI-газета: собрать новости → отобрать и перевести через Claude → сверстать PDF → отправить.

Запуск:
  python main.py            — обычный выпуск (так его запускает GitHub по расписанию)
  python main.py --no-send  — собрать и сверстать, но не отправлять и ничего не запоминать
  python main.py --demo     — сверстать пробный номер из demo/issue.json (без интернета и без API)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from gazeta import ai, render
from gazeta.state import State

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-газета")
    parser.add_argument("--no-send", action="store_true", help="не отправлять письма и не менять память")
    parser.add_argument("--demo", action="store_true", help="пробный номер из demo/issue.json")
    args = parser.parse_args()

    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    today = datetime.now(ZoneInfo(config["newspaper"].get("timezone", "Asia/Jerusalem")))
    state = State(ROOT / "state" / "state.json")
    number = state.issue + 1

    if args.demo:
        issue = json.loads((ROOT / "demo" / "issue.json").read_text(encoding="utf-8"))
        ctx = render.context(issue, config, number, today, {"n_items": 0, "n_sources": 0, "demo": True})
        pdf = render.render_pdf(ctx, OUT, ROOT, today)
        html, _ = render.render_email(ctx, ROOT)
        (OUT / "email.html").write_text(html, encoding="utf-8")
        print(f"Пробный номер: {pdf}")
        return 0

    from gazeta.collect import Collector  # сетевые зависимости — только для настоящего выпуска

    collector = Collector(config, state)
    items = collector.run()
    print("\n".join(collector.log))

    usages, picks = [], []
    issue = {"top": [], "articles": [], "try_this": []}
    if items:
        picks, usage = ai.select(items, config)
        usages.append(usage)
        print(f"Отобрано для номера: {len(picks)}")
        if picks:
            issue, usage = ai.write(items, picks, config, render.ru_date(today))
            usages.append(usage)
    print(f"Токены: {usages}")
    print(f"Примерная стоимость выпуска: ${ai.cost_usd(usages):.3f}")

    OUT.mkdir(exist_ok=True)
    (OUT / "issue.json").write_text(json.dumps(issue, ensure_ascii=False, indent=1), encoding="utf-8")

    stats = {
        "n_items": len(items),
        "n_sources": sum(1 for line in collector.log if line.startswith("ok")),
        "failed": collector.failed,
    }
    ctx = render.context(issue, config, number, today, stats)
    if ctx["lead"] or ctx["sections"]:
        pdf = render.render_pdf(ctx, OUT, ROOT, today)
        html, text = render.render_email(ctx, ROOT)
        subject = f"{config['newspaper'].get('name', 'AI-газета')} № {number} — {render.ru_date(today)}"
    else:
        pdf = None
        text = "Значимых релизов и находок за сутки не было."
        if collector.failed:
            text += "\nНе ответили: " + ", ".join(collector.failed)
        html = f"<p>{text}</p>".replace("\n", "<br>")
        subject = f"{config['newspaper'].get('name', 'AI-газета')} — {render.ru_date(today)}: значимых новостей нет"
        print(text)

    if args.no_send:
        print("Режим --no-send: письма не отправлены, память не изменена.")
        return 0

    from gazeta.mail import send_issue

    sent_to = send_issue(subject, html, text, pdf)
    print(f"Отправлено: {', '.join(sent_to)}")

    if pdf:
        state.issue = number
    state.mark_seen({items[i].key for p in picks for i in p["ids"]})
    state.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
