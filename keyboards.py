from aiogram.types import (InlineKeyboardButton as IB, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup)

# --- кнопки водителя
B_START = "🚚 Начать маршрут"
B_MY_ROUTE = "📋 Мой маршрут"
B_PROBLEM = "⚠️ Проблема на адресе"
B_INSTR = "📖 Инструкции"
B_SEND_PROBLEM = "📨 Отправить логисту"

# --- кнопки админа / заказчика
A_UPLOAD = "📥 Загрузить Excel"
A_ASSIGN = "🧭 Назначить маршруты"
A_STATS = "📊 Статистика"
A_REPORT = "📑 Отчёт"
A_EXPORT = "📤 Выгрузить Excel"
A_CLOSE = "🔒 Закрыть рейс"
A_ZIP = "🗜 Сохранить .ZIP"
A_MSG = "✉️ Сообщение водителям"
A_INSTR = "📖 Инструкции водителям"
A_INSTR_ADD = "➕ Добавить инструкцию"
A_USERS = "👥 Пользователи"
A_INVITE = "🎟 Пригласить"
A_PROBLEM_TEXT = "⚙️ Текст «Проблема»"
A_NOTES = "📝 Примечания"

CANCEL = "❌ Отмена"


def kb(rows, **kw) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
                               resize_keyboard=True, **kw)


def ikb(rows) -> InlineKeyboardMarkup:
    """rows: [[(text, callback_data), ...], ...]"""
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text=t, callback_data=d) for t, d in r] for r in rows])


DRIVER_KB = kb([[B_START], [B_MY_ROUTE, B_PROBLEM], [B_INSTR]])
ADMIN_KB = kb([
    [A_UPLOAD, A_ASSIGN],
    [A_STATS, A_REPORT],
    [A_EXPORT, A_CLOSE],
    [A_ZIP, A_MSG],
    [A_INSTR, A_INSTR_ADD],
    [A_USERS, A_INVITE],
    [A_NOTES, A_PROBLEM_TEXT],
])
CUSTOMER_KB = kb([[A_STATS, A_REPORT], [A_EXPORT]])
CANCEL_KB = kb([[CANCEL]])
PROBLEM_KB = kb([[B_SEND_PROBLEM], [CANCEL]])

DRIVER_BUTTONS = {B_START, B_MY_ROUTE, B_PROBLEM, B_INSTR}
ADMIN_BUTTONS = {A_UPLOAD, A_ASSIGN, A_STATS, A_REPORT, A_EXPORT, A_CLOSE, A_ZIP, A_MSG, A_INSTR, A_INSTR_ADD,
                 A_USERS, A_INVITE, A_PROBLEM_TEXT, A_NOTES}


def kb_for(role: str | None) -> ReplyKeyboardMarkup:
    return {"admin": ADMIN_KB, "customer": CUSTOMER_KB}.get(role, DRIVER_KB)


def short(text: str, n: int = 48) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"
