from aiogram.types import (InlineKeyboardButton as IB, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup)

# --- кнопки водителя
B_START = "🚚 Начать маршрут"
B_MY_ROUTE = "📋 Мой маршрут"
B_PROBLEM = "⚠️ Проблема на адресе"
B_INSTR = "📖 Инструкции"

# --- кнопки админа
A_UPLOAD = "📥 Загрузить Excel"
A_ASSIGN = "🧭 Назначить маршруты"
A_STATS = "📊 Статистика"
A_REPORT = "📑 Отчёт"
A_EXPORT = "📤 Выгрузить Excel"
A_INSTR = "📖 Инструкции водителям"
A_MSG = "✉️ Сообщение водителям"
A_DRIVERS = "👥 Водители"
A_PROBLEM_TEXT = "⚙️ Текст «Проблема»"

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
    [A_EXPORT, A_INSTR],
    [A_MSG, A_DRIVERS],
    [A_PROBLEM_TEXT],
])
CANCEL_KB = kb([[CANCEL]])

DRIVER_BUTTONS = {B_START, B_MY_ROUTE, B_PROBLEM, B_INSTR}
ADMIN_BUTTONS = {A_UPLOAD, A_ASSIGN, A_STATS, A_REPORT, A_EXPORT, A_INSTR, A_MSG, A_DRIVERS, A_PROBLEM_TEXT}


def short(text: str, n: int = 48) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"
