"""Резервные копии данных бота в Telegram и восстановление из них.

Копия = zip с базой bot.db (все пользователи, маршруты, точки, ссылки на фото, примечания, заметки,
инструкции, настройки) и исходными Excel-файлами. В BACKUP_TIMES (по умолчанию 09:00 и 21:00) бот присылает
копию в канал резервных копий, а если канал не подключён — администраторам из ADMIN_IDS. Даже если хостинг потеряет файлы, данные восстанавливаются из последней копии.
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
from config import ADMIN_IDS, BACKUP_CHANNEL_ID, BACKUP_TIMES, DATA_DIR, DB_PATH, WB_DIR

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


def channel_id() -> int | None:
    """Канал резервных копий: из BACKUP_CHAT_ID или подключённый кнопкой в боте."""
    if BACKUP_CHANNEL_ID:
        return BACKUP_CHANNEL_ID
    v = db.get_setting("backup_channel_id")
    return int(v) if v.lstrip("-").isdigit() else None


async def send_backup(bot: Bot, reason: str, chat_ids: set[int] | None = None) -> list[str]:
    """Отправляет копию; возвращает список ошибок (пустой — всё доставлено)."""
    path, meta = make_backup()
    errors = []
    try:
        c = meta["counts"]
        caption = (f"💾 Резервная копия данных бота ({reason})\n"
                   f"{meta['created_at'][:16]}: " + ", ".join(f"{k}: {v}" for k, v in c.items()) +
                   "\n\nВосстановление: отправьте этот файл боту.")
        for cid in (chat_ids or ADMIN_IDS):
            try:
                await bot.send_document(cid, FSInputFile(path, filename=path.name), caption=caption,
                                        disable_notification=True)
            except Exception as e:  # noqa: BLE001
                log.warning("backup to %s failed: %s", cid, e)
                errors.append(f"{cid}: {e}")
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)
    return errors


def next_run(now: datetime) -> tuple[datetime, str]:
    best = None
    for t in BACKUP_TIMES:
        hh, mm = map(int, t.split(":"))
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        if best is None or cand < best[0]:
            best = (cand, f"{hh:02d}:{mm:02d}")
    return best


async def daily_loop(bot: Bot) -> None:
    """Плановые копии в BACKUP_TIMES (по часовому поясу бота) — в канал или администраторам."""
    if not BACKUP_TIMES:
        return
    while True:
        nxt, label = next_run(db.now())
        await asyncio.sleep((nxt - db.now()).total_seconds())
        try:
            if not is_empty():
                ch = channel_id()
                errors = await send_backup(bot, f"плановая, {label}", {ch} if ch else None)
                if ch and errors:  # канал недоступен — чтобы копия не потерялась, шлём администраторам
                    await send_backup(bot, f"плановая, {label}; канал недоступен: {errors[0][:150]}")
        except Exception as e:  # noqa: BLE001
            log.exception("scheduled backup failed: %s", e)
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


# ------------------------------------------------------------------ автокопия ---
# Закреплённая копия, которая обновляется после изменений данных. Хранится в двух местах:
#  • личный чат бота с первым администратором из ADMIN_IDS — там бот может закреплять всегда;
#  • канал BACKUP_CHAT_ID (если задан) — для закрепления боту нужно право «Редактирование сообщений».
# Если хостинг стёр базу, бот при запуске находит самую свежую закреплённую копию и восстанавливается.

from aiogram.types import InputMediaDocument  # noqa: E402

from config import ADMIN_LIST, AUTOSAVE_MINUTES  # noqa: E402

AUTO_CAPTION = ("💾 <b>Автокопия данных бота</b> — обновляется автоматически.\n"
                "Не удаляйте и не открепляйте это сообщение: по нему бот восстановит данные, "
                "если хостинг их потеряет.\n")
_auto: dict[int, dict] = {}       # chat_id -> {"msg_id", "day"}
_pin_warned: set[int] = set()
_lock = asyncio.Lock()


def autosave_chats() -> list[int]:
    chats = []
    if ADMIN_LIST:
        chats.append(ADMIN_LIST[0])
    if BACKUP_CHANNEL_ID and BACKUP_CHANNEL_ID not in chats:
        chats.append(BACKUP_CHANNEL_ID)
    return chats


def restore_candidates() -> list[int]:
    """Где искать закреплённую копию при пустой базе: канал из настроек и личные чаты всех админов."""
    out = []
    for c in ([BACKUP_CHANNEL_ID] if BACKUP_CHANNEL_ID else []) + list(ADMIN_LIST):
        if c not in out:
            out.append(c)
    return out


def _caption(meta: dict) -> str:
    return AUTO_CAPTION + f"Обновлено: {meta['created_at'][:16]}\n" + \
        ", ".join(f"{k}: {v}" for k, v in meta["counts"].items())


async def find_pinned(bot: Bot, chat_id: int):
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as e:  # noqa: BLE001
        log.warning("get_chat(%s) failed: %s", chat_id, e)
        return None
    pm = chat.pinned_message
    if pm and pm.document and (pm.document.file_name or "").startswith(PREFIX):
        return pm
    return None


async def _save_to(bot: Bot, chat_id: int, path: Path, meta: dict) -> None:
    st = _auto.setdefault(chat_id, {"msg_id": None, "day": None})
    today = db.now().date().isoformat()
    if st["msg_id"] and st["day"] == today:
        try:
            await bot.edit_message_media(
                InputMediaDocument(media=FSInputFile(path, filename=path.name), caption=_caption(meta)),
                chat_id=chat_id, message_id=st["msg_id"])
            return
        except Exception as e:  # noqa: BLE001
            if "not modified" in str(e):
                return
            log.warning("autosave edit in %s failed, sending new: %s", chat_id, e)
    # раз в день и после запуска — новое сообщение (чтобы оно было последним закреплённым), старое удаляем
    m = await bot.send_document(chat_id, FSInputFile(path, filename=path.name),
                                caption=_caption(meta), disable_notification=True)
    try:
        await bot.pin_chat_message(chat_id, m.message_id, disable_notification=True)
    except Exception as e:  # noqa: BLE001
        log.warning("pin in %s failed: %s", chat_id, e)
        if chat_id not in _pin_warned and ADMIN_LIST:
            _pin_warned.add(chat_id)
            try:
                await bot.send_message(ADMIN_LIST[0], f"⚠️ Бот не смог закрепить автокопию в чате <code>{chat_id}</code>: "
                                                      f"{str(e)[:150]}\nДля канала дайте боту право "
                                                      f"«Редактирование сообщений». Копия в вашем личном чате "
                                                      f"с ботом при этом работает.")
            except Exception:
                pass
    if st["msg_id"] and st["msg_id"] != m.message_id:
        try:
            await bot.delete_message(chat_id, st["msg_id"])
        except Exception:
            pass
    st["msg_id"], st["day"] = m.message_id, today


async def autosave(bot: Bot, force: bool = False) -> None:
    """Обновляет закреплённую автокопию во всех местах, если данные менялись (или force)."""
    chats = autosave_chats()
    if not chats or (not db.dirty and not force) or is_empty():
        return
    async with _lock:
        db.dirty = False
        path, meta = make_backup()
        try:
            for cid in chats:
                try:
                    await _save_to(bot, cid, path, meta)
                except Exception as e:  # noqa: BLE001
                    db.dirty = True
                    log.exception("autosave to %s failed: %s", cid, e)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


async def autosave_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(AUTOSAVE_MINUTES * 60)
        await autosave(bot)


async def startup_restore(bot: Bot) -> str | None:
    """При запуске: запоминает закреплённые автокопии; если база пустая — восстанавливает из самой свежей.
    Возвращает дату копии, если восстановление произошло."""
    found = []
    for cid in restore_candidates():
        pm = await find_pinned(bot, cid)
        if pm:
            found.append((pm.document.file_name, cid, pm))
            if pm.from_user and pm.from_user.is_bot and cid in autosave_chats():
                _auto[cid] = {"msg_id": pm.message_id, "day": None}
    if not found or not is_empty():
        return None
    found.sort(key=lambda x: x[0], reverse=True)   # имя содержит дату и время: самая свежая — первая
    for _, cid, pm in found:
        tmp = DATA_DIR / "autorestore.zip"
        try:
            await bot.download(pm.document, destination=tmp)
            meta = inspect(tmp)
            restore(tmp)
            db.dirty = False
            return str(meta.get("created_at", "?"))[:16]
        except Exception as e:  # noqa: BLE001
            log.warning("restore from %s failed: %s", cid, e)
        finally:
            tmp.unlink(missing_ok=True)
    return None
