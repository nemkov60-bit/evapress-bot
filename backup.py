"""Резервные копии данных бота в Telegram и восстановление из них.

Копия = zip с базой bot.db (все пользователи, маршруты, точки, ссылки на фото, примечания, заметки,
инструкции, настройки) и исходными Excel-файлами. Каждый день в BACKUP_TIME бот присылает копию
администраторам из ADMIN_IDS. Даже если хостинг потеряет файлы, данные восстанавливаются из последней копии.
Фото хранятся на серверах Telegram, в копии — ссылки на них, поэтому архив маленький.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

import db
from config import ADMIN_IDS, BACKUP_TIME, DATA_DIR, DB_PATH, WB_DIR

PREFIX = "evapress_backup_"
MAX_SIZE = 45 * 1024 * 1024
log = logging.getLogger("backup")


def counts(conn: sqlite3.Connection) -> dict:
    res = {}
    for t, title in (("users", "пользователей"), ("workbooks", "дат"), ("points", "точек"),
                     ("notes", "примечаний и заметок"), ("instructions", "инструкций")):
        try:
            res[title] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            res[title] = 0
    return res


def is_empty() -> bool:
    c = counts(db.conn)
    return c["пользователей"] == 0 and c["дат"] == 0


def make_backup() -> tuple[Path, dict]:
    tmp = Path(tempfile.mkdtemp(prefix="bk_", dir=DATA_DIR))
    snap = tmp / "bot.db"
    dst = sqlite3.connect(snap)
    db.conn.backup(dst)          # согласованный снимок, даже если бот сейчас пишет в базу
    stats = counts(dst)
    dst.close()
    name = f"{PREFIX}{db.now():%Y-%m-%d_%H%M}.zip"
    path = tmp / name
    # свежие Excel первыми; если архив упирается в лимит Telegram, старые файлы не кладём
    files = sorted(WB_DIR.glob("*.xls*"), key=lambda p: p.stat().st_mtime, reverse=True)
    size = snap.stat().st_size
    skipped = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(snap, "bot.db")
        for f in files:
            if size + f.stat().st_size > MAX_SIZE:
                skipped += 1
                continue
            z.write(f, f"workbooks/{f.name}")
            size += f.stat().st_size
        meta = {"created_at": db.now_s(), "counts": stats, "skipped_workbooks": skipped}
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
    snap.unlink()
    return path, meta


async def send_backup(bot: Bot, reason: str, chat_ids: set[int] | None = None) -> None:
    path, meta = make_backup()
    try:
        c = meta["counts"]
        caption = (f"💾 Резервная копия данных бота ({reason})\n"
                   f"{meta['created_at'][:16]}: " + ", ".join(f"{k}: {v}" for k, v in c.items()) +
                   "\n\nСохраните файл. Восстановление: отправьте его боту с подписью /restore")
        for cid in (chat_ids or ADMIN_IDS):
            try:
                await bot.send_document(cid, FSInputFile(path, filename=path.name), caption=caption,
                                        disable_notification=True)
            except Exception as e:  # noqa: BLE001
                log.warning("backup to %s failed: %s", cid, e)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


async def daily_loop(bot: Bot) -> None:
    """Ежедневная копия в BACKUP_TIME (по часовому поясу бота)."""
    if not BACKUP_TIME:
        return
    hh, mm = map(int, BACKUP_TIME.split(":"))
    while True:
        now = db.now()
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            if not is_empty():
                await send_backup(bot, "ежедневная")
        except Exception as e:  # noqa: BLE001
            log.exception("daily backup failed: %s", e)
        await asyncio.sleep(61)


def inspect(zip_path: Path) -> dict:
    """Проверяет архив и возвращает его meta; ValueError, если это не копия бота."""
    with zipfile.ZipFile(zip_path) as z:
        if "bot.db" not in z.namelist():
            raise ValueError("в архиве нет bot.db — это не резервная копия бота")
        tmp = Path(tempfile.mkdtemp(prefix="chk_", dir=DATA_DIR))
        try:
            z.extract("bot.db", tmp)
            conn = sqlite3.connect(tmp / "bot.db")
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("база в архиве повреждена")
                c = counts(conn)
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        meta = json.loads(z.read("meta.json")) if "meta.json" in z.namelist() else {}
        meta["counts"] = c
        return meta


def restore(zip_path: Path) -> Path:
    """Заменяет базу и Excel-файлы данными из архива. Текущая база сохраняется рядом — её путь возвращается."""
    inspect(zip_path)
    safety = DATA_DIR / f"before_restore_{datetime.now():%Y%m%d_%H%M%S}.db"
    cur = sqlite3.connect(safety)
    db.conn.backup(cur)
    cur.close()
    with zipfile.ZipFile(zip_path) as z:
        tmp = Path(tempfile.mkdtemp(prefix="rst_", dir=DATA_DIR))
        z.extractall(tmp)
    db.conn.close()
    shutil.copy(tmp / "bot.db", DB_PATH)
    WB_DIR.mkdir(parents=True, exist_ok=True)
    for f in (tmp / "workbooks").glob("*") if (tmp / "workbooks").exists() else []:
        shutil.copy(f, WB_DIR / f.name)
    shutil.rmtree(tmp, ignore_errors=True)
    db.reload()
    # пути к Excel могли поменяться (другой сервер / папка) — переписываем на текущую папку
    for w in db.q("SELECT id, path FROM workbooks"):
        db.x("UPDATE workbooks SET path=? WHERE id=?", (str(WB_DIR / Path(w["path"]).name), w["id"]))
    return safety
