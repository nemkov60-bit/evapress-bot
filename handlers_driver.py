"""Сценарии водителя."""
from html import escape as h

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import db
import services as S
from common import extract_content, is_admin, notify_admins, send_content, user_label
from keyboards import (DRIVER_BUTTONS, B_INSTR, B_MY_ROUTE, B_PROBLEM, B_START, CANCEL, CANCEL_KB, DRIVER_KB, ADMIN_KB,
                       ikb, short)

router = Router(name="driver")


class Reg(StatesGroup):
    name = State()


class PointFlow(StatesGroup):
    photo = State()


class ProblemFlow(StatesGroup):
    text = State()


NOT_MENU = ~F.text.in_(DRIVER_BUTTONS | {CANCEL})

STEP_PROMPTS = {
    "display": "📸 <b>Фото 1 из 3:</b> выкладка продукции.\nОтправьте фотографию.",
    "invoice": "📸 <b>Фото 2 из 3:</b> накладная с печатью и подписью грузополучателя.\nОтправьте фотографию.",
    "poa": "📸 <b>Фото 3 из 3:</b> доверенность на возврат.\n"
           "Отправьте фотографию или нажмите «Доверенность отсутствует».",
}
STEP_FIELD = {"display": "photo_display", "invoice": "photo_invoice", "poa": "photo_poa"}


# ------------------------------------------------------------ helpers -----

def get_driver(uid: int):
    return db.q1("SELECT * FROM users WHERE tg_id=? AND active=1", (uid,))


def own_point(uid: int, pid: int):
    """Точка, если она на маршруте этого водителя."""
    return db.q1("SELECT p.* FROM points p JOIN routes r ON r.id=p.route_id WHERE p.id=? AND r.driver_id=?",
                 (pid, uid))


def own_route(uid: int, rid: int):
    r = S.route_with_wb(rid)
    return r if r and r["driver_id"] == uid else None


def route_view(rid: int):
    r = S.route_with_wb(rid)
    c, t = S.route_counts(rid)
    text = (f"🗺 <b>Маршрут №{h(r['route_no'])} на {S.fmt_date(r['work_date'])}</b>\n"
            f"Закрыто точек: <b>{c} из {t}</b>\n\n"
            f"Нажмите на точку, чтобы посмотреть подробности и открыть её.")
    rows = []
    for p in db.q("SELECT * FROM points WHERE route_id=? ORDER BY seq", (rid,)):
        icon = "✅" if p["status"] == "closed" else ("🟡" if p["opened_at"] else "⬜")
        rows.append([(f"{icon} {p['seq']}. {short(p['address'], 45)}", f"p:{p['id']}")])
    rows.append([("🔄 Обновить", f"r:{rid}")])
    return text, ikb(rows)


def point_view(p):
    text = S.point_text(p)
    rows = []
    if p["status"] != "closed":
        rows.append([("📍 Открыть точку", f"po:{p['id']}")])
    rows.append([("⚠️ Проблема на адресе", f"pp:{p['id']}")])
    rows.append([("⬅️ К списку точек", f"r:{p['route_id']}")])
    return text, ikb(rows)


async def ask_step(message: Message, state: FSMContext, p):
    """Показывает следующий шаг закрытия точки."""
    step = S.next_step(p)
    if step:
        await state.set_state(PointFlow.photo)
        await state.update_data(point_id=p["id"])
        rows = []
        if step == "poa":
            rows.append([("🚫 Доверенность отсутствует", f"na:{p['id']}")])
        rows.append([("⬅️ К списку точек", f"r:{p['route_id']}")])
        await message.answer(f"<b>{h(short(p['address'], 80))}</b>\n\n{S.photos_progress(p)}\n\n{STEP_PROMPTS[step]}",
                             reply_markup=ikb(rows))
    else:
        await state.clear()
        await message.answer(
            f"<b>{h(short(p['address'], 80))}</b>\n\n{S.photos_progress(p)}\n\n"
            f"Все фото загружены. Нажмите «Закрыть точку».",
            reply_markup=ikb([[("✅ Закрыть точку", f"pc:{p['id']}")],
                              [("🔁 Переснять фото", f"pr:{p['id']}")],
                              [("⬅️ К списку точек", f"r:{p['route_id']}")]]))


# ------------------------------------------------------------ start/reg ---

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if is_admin(uid):
        await message.answer("👋 Добро пожаловать, администратор!\n"
                             "Загрузите Excel с маршрутами (просто отправьте файл) или выберите действие в меню.\n"
                             "Справка: /help", reply_markup=ADMIN_KB)
        return
    if get_driver(uid):
        await message.answer("👋 Здравствуйте! Выберите действие в меню.", reply_markup=DRIVER_KB)
        return
    await state.set_state(Reg.name)
    await message.answer("👋 Здравствуйте! Это бот транспортного логиста EVA Press.\n\n"
                         "Для регистрации введите ваши <b>Фамилию Имя Отчество</b>:")


@router.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Reg.name, F.text)
async def reg_name(message: Message, state: FSMContext, bot: Bot):
    name = " ".join(message.text.split())
    if len(name) < 5 or len(name.split()) < 2 or name.startswith("/"):
        await message.answer("Введите ФИО полностью, например: <i>Иванов Иван Иванович</i>")
        return
    db.x("INSERT INTO users(tg_id, full_name, username, created_at) VALUES(?,?,?,?) "
         "ON CONFLICT(tg_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username, active=1",
         (message.from_user.id, name, message.from_user.username, db.now_s()))
    await state.clear()
    await message.answer(f"✅ Вы зарегистрированы как <b>{h(name)}</b>.\n"
                         f"Когда логист назначит вам маршрут, нажмите «{B_START}».", reply_markup=DRIVER_KB)
    await notify_admins(bot, f"🆕 Зарегистрирован водитель: {user_label(message.from_user.id)}")


# ------------------------------------------------------------ guards ------

@router.message(F.func(lambda m: not get_driver(m.from_user.id)))
async def not_registered(message: Message, state: FSMContext):
    await start(message, state)


@router.callback_query(F.func(lambda c: not get_driver(c.from_user.id)))
async def not_registered_cb(cb: CallbackQuery):
    await cb.answer("Сначала зарегистрируйтесь: /start", show_alert=True)


# ------------------------------------------------------------ menu --------

@router.message(F.text == CANCEL)
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=DRIVER_KB)


@router.message(F.text == B_START)
async def start_route(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    routes = S.driver_routes(message.from_user.id)
    if not routes:
        await message.answer("На данный момент вам не назначен маршрут. Обратитесь к логисту.")
        return
    if len(routes) > 1:
        await message.answer("У вас несколько маршрутов. Выберите, какой начать:",
                             reply_markup=ikb([[(f"№{r['route_no']} на {S.fmt_date(r['work_date'])}", f"rs:{r['id']}")]
                                               for r in routes]))
        return
    await begin_route(message, routes[0]["id"], message.from_user.id, bot)


async def begin_route(message: Message, rid: int, uid: int, bot: Bot):
    r = S.route_with_wb(rid)
    if not r["started_at"]:
        db.x("UPDATE routes SET started_at=? WHERE id=?", (db.now_s(), rid))
        await notify_admins(bot, f"🚚 {user_label(uid)} начал маршрут №{h(r['route_no'])} "
                                 f"({S.fmt_date(r['work_date'])})")
    text, markup = route_view(rid)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("rs:"))
async def cb_route_start(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data[3:])
    if not own_route(cb.from_user.id, rid):
        await cb.answer("Маршрут вам не назначен", show_alert=True)
        return
    await cb.answer()
    await begin_route(cb.message, rid, cb.from_user.id, bot)


@router.message(F.text == B_MY_ROUTE)
async def my_route(message: Message, state: FSMContext):
    await state.clear()
    routes = S.driver_routes(message.from_user.id)
    if not routes:
        await message.answer("На данный момент вам не назначен маршрут.")
        return
    for r in routes:
        text, markup = route_view(r["id"])
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("r:"))
async def cb_route(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data[2:])
    if not own_route(cb.from_user.id, rid):
        await cb.answer("Маршрут вам больше не назначен", show_alert=True)
        return
    await state.clear()
    text, markup = route_view(rid)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("p:"))
async def cb_point(cb: CallbackQuery):
    p = own_point(cb.from_user.id, int(cb.data[2:]))
    if not p:
        await cb.answer("Точка не найдена", show_alert=True)
        return
    text, markup = point_view(p)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


# ------------------------------------------------------------ closing -----

@router.callback_query(F.data.startswith("po:"))
async def cb_open_point(cb: CallbackQuery, state: FSMContext):
    p = own_point(cb.from_user.id, int(cb.data[3:]))
    if not p:
        await cb.answer("Точка не найдена", show_alert=True)
        return
    if p["status"] == "closed":
        await cb.answer("Точка уже закрыта", show_alert=True)
        return
    if not p["opened_at"]:
        db.x("UPDATE points SET opened_at=? WHERE id=?", (db.now_s(), p["id"]))
        p = own_point(cb.from_user.id, p["id"])
    await cb.answer("Точка открыта")
    await ask_step(cb.message, state, p)


@router.message(PointFlow.photo, F.photo)
async def got_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    p = own_point(message.from_user.id, data.get("point_id", 0))
    if not p or p["status"] == "closed":
        await state.clear()
        await message.answer("Точка не найдена или уже закрыта.")
        return
    step = S.next_step(p)
    if step:
        # чтение и запись без await между ними — альбом из нескольких фото не «перетрёт» шаги
        db.x(f"UPDATE points SET {STEP_FIELD[step]}=? WHERE id=?", (message.photo[-1].file_id, p["id"]))
    p = own_point(message.from_user.id, p["id"])
    await ask_step(message, state, p)


@router.message(PointFlow.photo, F.document)
async def got_doc(message: Message):
    await message.answer("Пожалуйста, отправьте снимок как <b>фото</b> (не файлом).")


@router.message(PointFlow.photo, F.text, NOT_MENU)
async def got_text_instead(message: Message):
    await message.answer("Ожидается фотография. Чтобы выйти, нажмите «⬅️ К списку точек» или любую кнопку меню.")


@router.callback_query(F.data.startswith("na:"))
async def cb_no_poa(cb: CallbackQuery, state: FSMContext):
    p = own_point(cb.from_user.id, int(cb.data[3:]))
    if not p or p["status"] == "closed":
        await cb.answer("Точка не найдена или уже закрыта", show_alert=True)
        return
    if not p["photo_display"] or not p["photo_invoice"]:
        await cb.answer("Сначала загрузите фото выкладки и накладной", show_alert=True)
        return
    db.x("UPDATE points SET poa_absent=1, photo_poa=NULL WHERE id=?", (p["id"],))
    await cb.answer("Отмечено: доверенность отсутствует")
    await ask_step(cb.message, state, own_point(cb.from_user.id, p["id"]))


@router.callback_query(F.data.startswith("pr:"))
async def cb_reshoot(cb: CallbackQuery, state: FSMContext):
    p = own_point(cb.from_user.id, int(cb.data[3:]))
    if not p or p["status"] == "closed":
        await cb.answer("Точка не найдена или уже закрыта", show_alert=True)
        return
    db.x("UPDATE points SET photo_display=NULL, photo_invoice=NULL, photo_poa=NULL, poa_absent=0 WHERE id=?",
         (p["id"],))
    await cb.answer("Фото сброшены")
    await ask_step(cb.message, state, own_point(cb.from_user.id, p["id"]))


@router.callback_query(F.data.startswith("pc:"))
async def cb_close(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    p = own_point(uid, int(cb.data[3:]))
    if not p:
        await cb.answer("Точка не найдена", show_alert=True)
        return
    if p["status"] == "closed":
        await cb.answer("Точка уже закрыта")
        return
    if S.next_step(p):
        await cb.answer("Загрузите все фото перед закрытием точки", show_alert=True)
        await ask_step(cb.message, state, p)
        return
    db.x("UPDATE points SET status='closed', closed_at=?, closed_by=? WHERE id=?", (db.now_s(), uid, p["id"]))
    await state.clear()
    await cb.answer("Точка закрыта ✅")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    rid = p["route_id"]
    c, t = S.route_counts(rid)
    await cb.message.answer(f"✅ Точка <b>{h(short(p['address'], 80))}</b> закрыта. Закрыто {c} из {t}.")
    if c == t:
        r = S.route_with_wb(rid)
        db.x("UPDATE routes SET finished_at=? WHERE id=?", (db.now_s(), rid))
        await cb.message.answer("🎉 Все точки маршрута закрыты. Маршрут выполнен! Спасибо!")
        await notify_admins(bot, f"🏁 {user_label(uid)} выполнил маршрут №{h(r['route_no'])} "
                                 f"({S.fmt_date(r['work_date'])}): закрыто {c} из {t}.")
    else:
        text, markup = route_view(rid)
        await cb.message.answer(text, reply_markup=markup)


# ------------------------------------------------------------ problem -----

@router.message(F.text == B_PROBLEM)
async def problem_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(db.get_setting("problem_help", S.DEFAULT_PROBLEM_HELP))
    routes = S.driver_routes(message.from_user.id)
    rows = []
    for r in routes:
        for p in db.q("SELECT * FROM points WHERE route_id=? AND status!='closed' ORDER BY seq", (r["id"],)):
            rows.append([(f"{p['seq']}. {short(p['address'], 45)}", f"pp:{p['id']}")])
    rows.append([("📝 Без привязки к адресу", "pp:0")])
    await message.answer("На каком адресе проблема?", reply_markup=ikb(rows[:95]))


@router.callback_query(F.data.startswith("pp:"))
async def cb_problem_point(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data[3:])
    addr = "без привязки к адресу"
    if pid:
        p = own_point(cb.from_user.id, pid)
        if not p:
            await cb.answer("Точка не найдена", show_alert=True)
            return
        addr = p["address"]
        if (cb.message.text or "").startswith("📍"):  # нажато из карточки точки — показываем памятку
            await cb.message.answer(db.get_setting("problem_help", S.DEFAULT_PROBLEM_HELP))
    await state.set_state(ProblemFlow.text)
    await state.update_data(problem_point=pid)
    await cb.answer()
    await cb.message.answer(f"Адрес: <b>{h(addr)}</b>\n\n✍️ Опишите ситуацию одним сообщением "
                            f"(можно приложить фото с подписью). Логист получит его сразу.",
                            reply_markup=CANCEL_KB)


@router.message(ProblemFlow.text, NOT_MENU)
async def problem_text(message: Message, state: FSMContext, bot: Bot):
    text, file_id, file_type = extract_content(message)
    if not text and not file_id:
        await message.answer("Отправьте текст или фото с описанием.")
        return
    data = await state.get_data()
    pid = data.get("problem_point") or None
    uid = message.from_user.id
    p = own_point(uid, pid) if pid else None
    rid = p["route_id"] if p else (S.driver_routes(uid)[0]["id"] if S.driver_routes(uid) else None)
    plain = message.text or message.caption or ""
    db.x("INSERT INTO problems(point_id, route_id, driver_id, text, file_id, file_type, created_at) "
         "VALUES(?,?,?,?,?,?,?)", (p["id"] if p else None, rid, uid, plain, file_id, file_type, db.now_s()))
    await state.clear()
    await message.answer("✅ Сообщение отправлено логисту. Ожидайте ответа.", reply_markup=DRIVER_KB)
    r = S.route_with_wb(rid) if rid else None
    head = (f"⚠️ <b>Проблема на адресе</b>\n"
            f"👤 {user_label(uid)}\n"
            + (f"🗺 Маршрут №{h(r['route_no'])} ({S.fmt_date(r['work_date'])})\n" if r else "")
            + (f"📍 {p['seq']}. {h(p['network'])} — {h(p['address'])}\n" if p else "📍 без привязки к адресу\n")
            + f"\n{text or '(без текста)'}")
    await notify_admins(bot, head, reply_markup=ikb([[("✉️ Ответить водителю", f"ans:{uid}")]]),
                        file_id=file_id, file_type=file_type)


# ------------------------------------------------------------ instructions

@router.message(F.text == B_INSTR)
async def instructions(message: Message, state: FSMContext):
    await state.clear()
    rows = db.q("SELECT id, title FROM instructions ORDER BY id")
    if not rows:
        await message.answer("Инструкций пока нет.")
        return
    await message.answer("📖 Инструкции для водителей:",
                         reply_markup=ikb([[(short(r["title"], 60), f"i:{r['id']}")] for r in rows]))


@router.callback_query(F.data.startswith("i:"))
async def cb_instruction(cb: CallbackQuery, bot: Bot):
    r = db.q1("SELECT * FROM instructions WHERE id=?", (int(cb.data[2:]),))
    await cb.answer()
    if not r:
        await cb.message.answer("Инструкция удалена.")
        return
    await send_content(bot, cb.from_user.id, f"📖 <b>{h(r['title'])}</b>\n\n{r['body'] or ''}",
                       r["file_id"], r["file_type"])


@router.message()
async def fallback(message: Message):
    await message.answer("Выберите действие в меню 👇", reply_markup=DRIVER_KB)
