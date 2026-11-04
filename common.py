"""Общие функции отправки сообщений."""
from html import escape as h

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup

import db
from config import ADMIN_IDS, BASE_DIR

POSTERS = {"start": BASE_DIR / "assets" / "poster_start.jpg",
           "thanks": BASE_DIR / "assets" / "poster_thanks.jpg"}


async def send_poster(bot: Bot, chat_id: int, key: str, caption: str, reply_markup=None) -> None:
    """Постер с подписью. После первой отправки Telegram даёт file_id — дальше шлём по нему, без повторной загрузки."""
    cache_key = f"poster_file_{key}"
    fid = db.get_setting(cache_key)
    if fid:
        try:
            await bot.send_photo(chat_id, fid, caption=caption, reply_markup=reply_markup)
            return
        except Exception:
            pass
    path = POSTERS[key]
    if not path.exists():
        await bot.send_message(chat_id, caption, reply_markup=reply_markup)
        return
    msg = await bot.send_photo(chat_id, FSInputFile(path), caption=caption, reply_markup=reply_markup)
    if msg and getattr(msg, "photo", None):
        db.set_setting(cache_key, msg.photo[-1].file_id)


ROLE_NAMES = {"admin": "Администратор", "customer": "Заказчик", "driver": "Водитель"}


def role_of(uid: int) -> str | None:
    """admin / customer / driver или None (нет доступа)."""
    if uid in ADMIN_IDS:
        return "admin"
    u = db.q1("SELECT role FROM users WHERE tg_id=? AND active=1", (uid,))
    return (u["role"] or "driver") if u else None


def is_admin(uid: int) -> bool:
    return role_of(uid) == "admin"


def is_staff(uid: int) -> bool:
    """Администратор или заказчик (наблюдатель)."""
    return role_of(uid) in ("admin", "customer")


def admin_ids() -> set[int]:
    return set(ADMIN_IDS) | {r["tg_id"] for r in db.q("SELECT tg_id FROM users WHERE role='admin' AND active=1")}


async def notify_admins(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None,
                        file_id: str | None = None, file_type: str | None = None,
                        exclude: set[int] | None = None) -> None:
    for aid in admin_ids() - (exclude or set()):
        try:
            await send_content(bot, aid, text, file_id, file_type, reply_markup)
        except Exception:  # админ не запускал бота и т.п.
            pass


async def send_content(bot: Bot, chat_id: int, text: str, file_id: str | None = None,
                       file_type: str | None = None, reply_markup=None) -> None:
    """Текст + (необязательно) фото/документ. Длинные подписи отправляются отдельным сообщением."""
    if file_id:
        caption = text if len(text) <= 1024 else None
        if file_type == "photo":
            await bot.send_photo(chat_id, file_id, caption=caption,
                                 reply_markup=reply_markup if caption else None)
        elif file_type == "video":
            await bot.send_video(chat_id, file_id, caption=caption,
                                 reply_markup=reply_markup if caption else None)
        else:
            await bot.send_document(chat_id, file_id, caption=caption,
                                    reply_markup=reply_markup if caption else None)
        if caption is not None:
            return
    for i in range(0, max(len(text), 1), 4000):
        last = i + 4000 >= len(text)
        await bot.send_message(chat_id, text[i:i + 4000] or "—", reply_markup=reply_markup if last else None)


def extract_content(message) -> tuple[str, str | None, str | None]:
    """(html-текст, file_id, тип) из сообщения пользователя"""
    text = message.html_text if (message.text or message.caption) else ""
    if message.photo:
        return text, message.photo[-1].file_id, "photo"
    if message.video:
        return text, message.video.file_id, "video"
    if message.document:
        return text, message.document.file_id, "document"
    return text, None, None


def user_label(uid: int) -> str:
    u = db.q1("SELECT * FROM users WHERE tg_id=?", (uid,))
    if not u:
        return f"id {uid}"
    s = h(u["full_name"])
    if u["offline"]:
        s += " (без Telegram)"
    if u["username"]:
        s += f" (@{h(u['username'])})"
    return s
