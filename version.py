"""Версия бота для команды /version. RELEASE обновляется при каждой доработке."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime

from config import BASE_DIR, TZ

RELEASE = "12.11.2026 — команда /version; «🖼 Загруженные фото» в запросе на закрытие точки"
STARTED = datetime.now(TZ)


def _git(*args: str) -> str | None:
    try:
        env = {**os.environ, "TZ": "Europe/Moscow"}   # время коммита — по Москве
        out = subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True, timeout=5, env=env)
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except Exception:
        return None


def text() -> str:
    commit = _git("rev-parse", "--short", "HEAD")
    date = _git("log", "-1", "--format=%cd", "--date=format-local:%d.%m.%Y %H:%M")
    lines = ["ℹ️ <b>Версия бота</b>", f"Выпуск: {RELEASE}"]
    if commit:
        lines.append(f"Коммит: <code>{commit}</code>" + (f" от {date} МСК" if date else ""))
    lines.append(f"Запущен: {STARTED:%d.%m.%Y %H:%M} МСК")
    return "\n".join(lines)
