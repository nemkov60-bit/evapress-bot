"""Общие функции отправки сообщений."""
from html import escape as h

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

import db
from config import ADMIN_IDS


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


async def notify_admins(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None,
                        file_id: str | None = None, file_type: str | None = None) -> None:
    for aid in ADMIN_IDS:
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
    if u["username"]:
        s += f" (@{h(u['username'])})"
    return s
