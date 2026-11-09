"""Отправка письма через Gmail (пароль приложения) и, по желанию, на email принтера."""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def _env(name: str, required: bool = True) -> str:
    value = (os.environ.get(name) or "").strip()
    if required and not value:
        raise RuntimeError(f"Не задан секрет {name}")
    return value


def send_issue(subject: str, html: str, text: str, pdf: Path | None) -> list[str]:
    sender = _env("GMAIL_ADDRESS")
    password = _env("GMAIL_APP_PASSWORD").replace(" ", "")
    to = [a.strip() for a in (_env("MAIL_TO", required=False) or sender).split(",") if a.strip()]
    printer = _env("PRINTER_EMAIL", required=False)

    messages = []
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"AI-газета <{sender}>", ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    if pdf:
        msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    messages.append(msg)

    if printer and pdf:  # принтеру — только PDF, без текста письма, чтобы не печатать лишнюю страницу
        pm = EmailMessage()
        pm["Subject"], pm["From"], pm["To"] = pdf.stem, sender, printer
        pm.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
        messages.append(pm)

    sent = []
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(sender, password)
        for m in messages:
            smtp.send_message(m)
            sent.append(m["To"])
    return sent
