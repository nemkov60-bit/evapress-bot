"""Подтверждения: ознакомление водителей с инструкциями и получение сообщений/примечаний логиста."""
from __future__ import annotations

import asyncio
from html import escape as h

from aiogram import Bot

import db
import services as S
from common import send_content
from keyboards import ikb


# ------------------------------------------------------------ инструкции --

def instr_text(i, new: bool = False) -> str:
    return (("🆕 " if new else "") + f"📖 <b>{h(i['title'])}</b>\n\n{i['body'] or ''}")


async def send_instruction(bot: Bot, uid: int, i, ask_ack: bool = True) -> None:
    markup = ikb([[("✅ Ознакомлен(а)", f"iok:{i['id']}")]]) if ask_ack else None
    await send_content(bot, uid, instr_text(i, ask_ack), i["file_id"], i["file_type"], reply_markup=markup)


async def send_unacked(bot: Bot, uid: int, intro: str) -> int:
    """Присылает водителю все инструкции, с которыми он не ознакомлен. Возвращает их число."""
    items = S.instr_unacked(uid)
    if not items:
        return 0
    await bot.send_message(uid, intro + f"\n\nИнструкций к ознакомлению: <b>{len(items)}</b>. "
                                        f"Под каждой нажмите «✅ Ознакомлен(а)».")
    for i in items:
        await send_instruction(bot, uid, i)
        await asyncio.sleep(0.1)
    return len(items)


async def broadcast_instruction(bot: Bot, iid: int, edited: bool) -> tuple[int, int]:
    """Новую/изменённую инструкцию — всем водителям, кто с этой версией не ознакомлен."""
    i = db.q1("SELECT * FROM instructions WHERE id=?", (iid,))
    sent = 0
    drivers = S.drivers()
    for d in drivers:
        if S.instr_is_acked(iid, d["tg_id"]):
            continue
        try:
            await bot.send_message(d["tg_id"], "📖 <b>Инструкция обновлена</b> — ознакомьтесь, пожалуйста:" if edited
                                   else "📖 <b>Новая инструкция</b> — ознакомьтесь, пожалуйста:")
            await send_instruction(bot, d["tg_id"], i)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return sent, len(drivers)


def instr_report() -> str:
    drivers = S.drivers()
    items = db.q("SELECT * FROM instructions ORDER BY id")
    if not items:
        return "Инструкций пока нет."
    lines = [f"📊 <b>Отчёт ознакомления с инструкциями</b>\nВодителей: {len(drivers)}", ""]
    for i in items:
        acked = [d for d in drivers if S.instr_is_acked(i["id"], d["tg_id"])]
        miss = [d for d in drivers if d not in acked]
        lines.append(f"<b>{h(i['title'])}</b> — ознакомлены {len(acked)} из {len(drivers)}"
                     + (" ✅" if not miss else ""))
        if miss:
            lines.append("   ⏳ " + ", ".join(h(d["full_name"]) for d in miss))
    lines.append("")
    lines.append("<b>По водителям:</b>")
    for d in drivers:
        n = len(S.instr_unacked(d["tg_id"]))
        lines.append(f"{'✅' if not n else '⏳'} {h(d['full_name'])}" + (f" — не ознакомлен(а): {n}" if n else ""))
    return "\n".join(lines)


# ------------------------------------------------------------ сообщения ---

ACK_BTN = "✅ Получено"


def ack_markup(mid: int, extra_rows: list | None = None):
    return ikb((extra_rows or []) + [[(ACK_BTN, f"mk:{mid}")]])


async def refresh_summary(bot: Bot, mid: int) -> None:
    m = db.q1("SELECT * FROM admin_messages WHERE id=?", (mid,))
    if not m or not m["admin_chat_id"] or not m["summary_msg_id"]:
        return
    try:
        await bot.edit_message_text(S.msg_status_text(mid), chat_id=m["admin_chat_id"],
                                    message_id=m["summary_msg_id"])
    except Exception:
        pass


async def post_summary(bot: Bot, mid: int) -> None:
    m = db.q1("SELECT * FROM admin_messages WHERE id=?", (mid,))
    try:
        msg = await bot.send_message(m["admin_chat_id"], S.msg_status_text(mid))
        db.x("UPDATE admin_messages SET summary_msg_id=? WHERE id=?", (msg.message_id, mid))
    except Exception:
        pass
