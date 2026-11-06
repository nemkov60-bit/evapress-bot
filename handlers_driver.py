"""Сценарии водителя."""
import asyncio
from html import escape as h

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message

import acks
import db
import guide
import geo
import notes as N
import services as S
from common import ROLE_NAMES, admin_ids, extract_content, notify_admins, role_of, send_content, send_poster, user_label
from keyboards import (DRIVER_BUTTONS, B_INSTR, B_MY_ROUTE, B_PROBLEM, B_SEND_PROBLEM, B_START, CANCEL, CANCEL_KB,
                       DRIVER_KB, PROBLEM_KB, ikb, kb_for, short)

router = Router(name="driver")


class Reg(StatesGroup):
    name = State()


class PointFlow(StatesGroup):
    photo = State()


class ProblemFlow(StatesGroup):
    text = State()


class NoteFlow(StatesGroup):
    text = State()


class SheetFlow(StatesGroup):
    photo = State()        # фото маршрутного листа перед началом маршрута


class PointReq(StatesGroup):
    comment = State()      # причина закрытия точки без фото


NOT_MENU = ~F.text.in_(DRIVER_BUTTONS | {CANCEL, B_SEND_PROBLEM})

PHOTO_HINT = "📌 <i>Следите за тем, чтобы фото были чёткие и читабельные.</i>"
STEP_PROMPTS = {
    "display": "📸 <b>Фото 1 из 3:</b> выкладка продукции.\nОтправьте фотографию.\n\n" + PHOTO_HINT,
    "invoice": "📸 <b>Фото 2 из 3:</b> накладная с печатью и подписью грузополучателя.\nОтправьте фотографию.\n\n"
               + PHOTO_HINT,
    "poa": "📸 <b>Фото 3 из 3:</b> доверенность на возврат.\n"
           "Отправьте фотографию или нажмите «Доверенность отсутствует».\n\n" + PHOTO_HINT,
}
DRIVER_WELCOME = ("Здравствуйте, уважаемый водитель!\n"
                  "С данным ботом выполнять доставку станет проще и удобнее.\n\n"
                  "Для регистрации введите ваши Имя и Фамилию:")

# Отложенный ответ: когда водитель шлёт альбом из 10 фото, бот отвечает один раз, а не 10
_pending: dict[int, asyncio.Task] = {}


def debounce(uid: int, coro_factory, delay: float = 1.5) -> None:
    old = _pending.pop(uid, None)
    if old:
        old.cancel()

    async def run():
        try:
            await asyncio.sleep(delay)
            _pending.pop(uid, None)
            await coro_factory()
        except asyncio.CancelledError:
            pass

    _pending[uid] = asyncio.create_task(run())


def media_of(message: Message) -> tuple[str, str, str | None] | None:
    """(file_id, тип, имя файла) для фото/видео/документа"""
    if message.photo:
        return message.photo[-1].file_id, "photo", None
    if message.video:
        return message.video.file_id, "video", message.video.file_name
    if message.document:
        return message.document.file_id, "document", message.document.file_name
    return None
STEP_FIELD = {"display": "photo_display", "invoice": "photo_invoice", "poa": "photo_poa"}


# ------------------------------------------------------------ helpers -----

CLOSED_TEXT = ("🔒 <b>Бот EVA Press закрытый.</b>\n"
               "Доступ только по приглашению. Попросите у администратора ссылку-приглашение "
               "и откройте её.")


def get_driver(uid: int):
    return db.q1("SELECT * FROM users WHERE tg_id=? AND active=1 AND COALESCE(role,'driver')='driver'", (uid,))


def own_point(uid: int, pid: int):
    """Точка, если она на маршруте этого водителя и рейс не закрыт."""
    return db.q1("SELECT p.* FROM points p JOIN routes r ON r.id=p.route_id JOIN workbooks w ON w.id=r.workbook_id "
                 "WHERE p.id=? AND r.driver_id=? AND w.closed_at IS NULL AND r.closed_at IS NULL", (pid, uid))


def own_route(uid: int, rid: int):
    r = S.route_with_wb(rid)
    return r if r and r["driver_id"] == uid and not r["wb_closed_at"] and not r["closed_at"] else None


def route_view(rid: int):
    r = S.route_with_wb(rid)
    c, t = S.route_counts(rid)
    text = (f"🗺 <b>Маршрут №{h(r['route_no'])}</b> · рейс {h(S.route_title(r))}\n"
            f"Закрыто точек: <b>{c} из {t}</b>\n"
            + "\nНажмите на точку, чтобы посмотреть подробности и открыть её. "
              "Когда все точки будут закрыты, маршрут закроется автоматически.")
    rblock = N.route_block(r["route_no"], r["work_date"])
    flagged = N.points_with_notes(rid, r["work_date"])
    if rblock:
        text = rblock + "\n\n" + text
    if flagged:
        text += "\n❗ — у точки есть важная информация или заметки водителей."
    rows = []
    for p in db.q("SELECT * FROM points WHERE route_id=? ORDER BY seq", (rid,)):
        icon = "✅" if p["status"] == "closed" else ("⏳" if p["close_req_at"] else ("🟡" if p["opened_at"] else "⬜"))
        mark = "❗" if p["id"] in flagged else ""
        rows.append([(f"{icon}{mark} {p['seq']}. {short(p['address'], 43)}", f"p:{p['id']}")])
    rows.append([("📄 Фото маршрутного листа", f"dsh:{rid}")])
    rows.append([("🔄 Обновить", f"r:{rid}")])
    return text, ikb(rows)


def point_view(p):
    r = S.route_with_wb(p["route_id"])
    block = N.point_block(p, r["work_date"])
    text = S.point_text(p, r) + ("\n\n" + block if block else "")
    rows = [[("🧭 Маршрут до точки", f"nav:{p['id']}")]]
    if p["status"] != "closed":
        rows.append([("📍 Открыть точку", f"po:{p['id']}")])
    rows.append([("⚠️ Проблема на адресе", f"pp:{p['id']}")])
    rows.append([("📝 Заметка к адресу", f"dn:{p['id']}"), ("🖼 Фото по точке", f"dph:{p['id']}")])
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
        if p["close_req_at"]:
            req = "\n\n⏳ <i>Запрос на закрытие точки без фото отправлен логисту — ожидайте решения.</i>"
        else:
            req = ""
            rows.append([("📨 Закрыть без фото (запрос логисту)", f"pq:{p['id']}")])
        rows.append([("⬅️ К списку точек", f"r:{p['route_id']}")])
        await message.answer(f"<b>{h(short(p['address'], 80))}</b>\n\n{S.photos_progress(p)}\n\n{STEP_PROMPTS[step]}"
                             + req, reply_markup=ikb(rows))
    else:
        # обязательные фото есть — остаёмся в режиме приёма: всё, что пришлёт водитель, — доп. фото
        await state.set_state(PointFlow.photo)
        await state.update_data(point_id=p["id"])
        await message.answer(
            f"<b>{h(short(p['address'], 80))}</b>\n\n{S.photos_progress(p)}\n\n"
            f"Обязательные фото загружены. Если ситуация нестандартная, отправьте дополнительные фото — "
            f"их количество не ограничено. Затем нажмите «Закрыть точку».\n\n{PHOTO_HINT}",
            reply_markup=ikb([[("✅ Закрыть точку", f"pc:{p['id']}")],
                              [("🔁 Переснять фото", f"pr:{p['id']}")],
                              [("⬅️ К списку точек", f"r:{p['route_id']}")]]))


# ------------------------------------------------------------ start/reg ---

@router.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject | None = None):
    await state.clear()
    uid = message.from_user.id
    if get_driver(uid):
        await send_poster(message.bot, message.chat.id, "start",
                          "👋 Здравствуйте, уважаемый водитель! Выберите действие в меню.", reply_markup=DRIVER_KB)
        return
    arg = (command.args or "") if command else ""
    if arg.startswith("inv_"):
        inv = S.check_invite(arg[4:])
        if not inv:
            await message.answer("❌ Приглашение недействительно: оно уже использовано, отозвано или истёк срок.\n"
                                 "Попросите у администратора новую ссылку.")
            return
        await state.set_state(Reg.name)
        await state.update_data(invite=inv["code"])
        if inv["role"] == "driver":
            await send_poster(message.bot, message.chat.id, "start", DRIVER_WELCOME)
        else:
            await message.answer(f"👋 Здравствуйте! Вас пригласили в бот транспортного логиста EVA Press "
                                 f"с ролью <b>{ROLE_NAMES.get(inv['role'], inv['role'])}</b>.\n\n"
                                 f"Для регистрации введите ваши Имя и Фамилию:")
        return
    await message.answer(CLOSED_TEXT)


@router.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Reg.name, F.text)
async def reg_name(message: Message, state: FSMContext, bot: Bot):
    name = " ".join(message.text.split())
    if len(name) < 5 or len(name.split()) < 2 or name.startswith("/"):
        await message.answer("Введите Имя и Фамилию полностью, например: <i>Иван Иванов</i>")
        return
    code = (await state.get_data()).get("invite")
    inv = S.check_invite(code)
    await state.clear()
    if not inv or not S.use_invite(code, message.from_user.id):
        await message.answer("❌ Приглашение недействительно. Попросите у администратора новую ссылку.")
        return
    role = inv["role"]
    db.x("INSERT INTO users(tg_id, full_name, username, role, created_at) VALUES(?,?,?,?,?) "
         "ON CONFLICT(tg_id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username, "
         "role=excluded.role, active=1",
         (message.from_user.id, name, message.from_user.username, role, db.now_s()))
    if inv["merge_uid"]:  # водитель без Telegram получил Telegram — переносим его историю
        S.merge_offline(inv["merge_uid"], message.from_user.id)
    hint = {
        "driver": f"Когда логист назначит вам маршрут, нажмите «{B_START}».",
        "customer": "Вам доступны статистика и отчёты по маршрутам. Справка: /help",
        "admin": "Вам доступны все функции администратора. Справка: /help",
    }[role]
    await message.answer(f"✅ Вы зарегистрированы как <b>{h(name)}</b> ({ROLE_NAMES[role]}).\n{hint}",
                         reply_markup=kb_for(role))
    uid = message.from_user.id
    inviter = inv["created_by"]
    rows = [[("👤 Карточка пользователя", f"u:{uid}")]]
    if role == "driver":
        wb = S.latest_workbook()
        if wb and not wb["closed_at"]:
            rows.insert(0, [(f"🧭 Назначить маршрут: {S.wb_title(wb)}", f"wd:as:{wb['id']}")])
    if role == "driver":
        await acks.send_unacked(bot, uid, "📖 <b>Добро пожаловать! Ознакомьтесь с инструкциями для водителей.</b>")
    await notify_admins(bot, f"🆕 <b>Новый пользователь по приглашению</b>\n"
                             f"👤 {user_label(uid)}, ID <code>{uid}</code>\n"
                             f"Роль: <b>{ROLE_NAMES[role]}</b>\n"
                             f"Пригласил(а): {user_label(inviter) if inviter else '—'}\n"
                             f"Зарегистрирован(а): {db.now():%d.%m.%Y %H:%M}",
                        reply_markup=ikb(rows), exclude={uid})


# ------------------------------------------------------------ guards ------

@router.message(F.func(lambda m: not get_driver(m.from_user.id)))
async def not_registered(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(CLOSED_TEXT)


@router.callback_query(F.func(lambda c: not get_driver(c.from_user.id)))
async def not_registered_cb(cb: CallbackQuery):
    await cb.answer("Нет доступа. Бот работает по приглашениям.", show_alert=True)


# ------------------------------------------------------------ menu --------

@router.message(F.text == CANCEL)
async def cancel(message: Message, state: FSMContext):
    _problem_buf.pop(message.from_user.id, None)
    await state.clear()
    await message.answer("Отменено.", reply_markup=DRIVER_KB)


@router.message(F.text == B_START)
async def start_route(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uid = message.from_user.id
    routes = S.driver_routes(uid)
    if not routes:
        await message.answer("На данный момент вам не назначен маршрут. Обратитесь к логисту.")
        return
    # 1) перед началом рейса — ознакомиться со всеми новыми инструкциями
    if await acks.send_unacked(bot, uid, "📖 <b>Перед началом рейса ознакомьтесь с инструкциями.</b>"):
        await message.answer(f"После ознакомления со всеми инструкциями снова нажмите «{B_START}».")
        return
    # 2) фото маршрутного листа — по каждому маршруту
    if await ask_next_sheet(message, state, uid):
        return
    # 3) все маршруты
    for r in routes:
        await begin_route(message, r["id"], uid, bot)


async def ask_next_sheet(message: Message, state: FSMContext, uid: int) -> bool:
    """Просит фото маршрутного листа для первого маршрута, где его нет. True — попросили."""
    for r in S.driver_routes(uid):
        if S.needs_sheet(r["id"]):
            await state.set_state(SheetFlow.photo)
            await state.update_data(sheet_rid=r["id"])
            await message.answer(f"📄 <b>Маршрут №{h(r['route_no'])}</b> · рейс {h(S.route_title(r))}\n\n"
                                 f"Сфотографируйте <b>маршрутный лист</b> и отправьте фото. Если лист на нескольких "
                                 f"страницах — отправьте все.\n\n{PHOTO_HINT}")
            return True
    return False


@router.message(SheetFlow.photo, F.photo | F.document)
async def got_sheet_photo(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    rid = (await state.get_data()).get("sheet_rid")
    r = own_route(uid, rid) if rid else None
    if not r:
        await state.clear()
        await message.answer("Маршрут недоступен.")
        return
    fid, ftype, fname = media_of(message)
    S.add_photo("sheet", fid, ftype, uid, route_id=rid, file_name=fname)

    async def reply():
        n = len(S.sheet_photos(rid))
        await message.answer(f"📄 Фото маршрутного листа №{h(r['route_no'])} получено ({n}).\n"
                             f"Если есть ещё страницы — отправьте их, затем нажмите «✅ Готово».",
                             reply_markup=ikb([[("✅ Готово", f"shd:{rid}")]]))

    debounce(uid, reply)


@router.message(SheetFlow.photo, NOT_MENU)
async def sheet_not_photo(message: Message):
    await message.answer("Отправьте фото маршрутного листа.")


@router.callback_query(F.data.startswith("shd:"))
async def cb_sheet_done(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    rid = int(cb.data[4:])
    if not own_route(uid, rid) or not S.sheet_photos(rid):
        await cb.answer("Сначала отправьте фото маршрутного листа", show_alert=True)
        return
    await cb.answer("Готово")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.clear()
    if await ask_next_sheet(cb.message, state, uid):
        return
    for r in S.driver_routes(uid):
        await begin_route(cb.message, r["id"], uid, bot)


async def begin_route(message: Message, rid: int, uid: int, bot: Bot):
    r = S.route_with_wb(rid)
    if not r["started_at"]:
        db.x("UPDATE routes SET started_at=? WHERE id=?", (db.now_s(), rid))
        n = len(S.sheet_photos(rid))
        await notify_admins(bot, f"🚚 {user_label(uid)} начал маршрут №{h(r['route_no'])} "
                                 f"(рейс {h(S.route_title(r))})" + (f", фото маршрутного листа: {n}" if n else ""))
    text, markup = route_view(rid)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("rs:"))
async def cb_route_start(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data[3:])
    if not own_route(cb.from_user.id, rid):
        await cb.answer("Маршрут вам не назначен", show_alert=True)
        return
    await cb.answer()
    if S.instr_unacked(cb.from_user.id) or S.needs_sheet(rid):
        await cb.message.answer(f"Нажмите «{B_START}» — бот проведёт по шагам перед началом маршрута.")
        return
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
        await cb.answer("Маршрут недоступен: рейс закрыт или маршрут снят", show_alert=True)
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
        await cb.answer("Точка недоступна: рейс закрыт или маршрут снят", show_alert=True)
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
        await cb.answer("Точка недоступна: рейс закрыт или маршрут снят", show_alert=True)
        return
    if S.needs_sheet(p["route_id"]) or S.instr_unacked(cb.from_user.id):
        await cb.answer(f"Сначала нажмите «{B_START}»: инструкции и фото маршрутного листа", show_alert=True)
        return
    if p["status"] == "closed":
        await cb.answer("Точка уже закрыта", show_alert=True)
        return
    if not p["opened_at"]:
        db.x("UPDATE points SET opened_at=? WHERE id=?", (db.now_s(), p["id"]))
        p = own_point(cb.from_user.id, p["id"])
    await cb.answer("Точка открыта")
    r = S.route_with_wb(p["route_id"])
    block = N.point_block(p, r["work_date"])
    if block:
        await cb.message.answer(f"📍 <b>{h(short(p['address'], 80))}</b>\n\n{block}")
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
    # чтение и запись без await между ними — альбом из нескольких фото не «перетрёт» шаги
    if step:
        db.x(f"UPDATE points SET {STEP_FIELD[step]}=? WHERE id=?", (message.photo[-1].file_id, p["id"]))
    else:
        S.add_photo("extra", message.photo[-1].file_id, "photo", message.from_user.id,
                    point_id=p["id"], route_id=p["route_id"])
    uid, pid = message.from_user.id, p["id"]

    async def reply():
        fresh = own_point(uid, pid)
        if fresh and fresh["status"] != "closed":
            await ask_step(message, state, fresh)

    debounce(uid, reply)


@router.message(PointFlow.photo, F.document | F.video)
async def got_doc(message: Message, state: FSMContext):
    data = await state.get_data()
    p = own_point(message.from_user.id, data.get("point_id", 0))
    if not p or p["status"] == "closed":
        await state.clear()
        await message.answer("Точка не найдена или уже закрыта.")
        return
    if S.next_step(p):
        await message.answer("Обязательные снимки отправьте как <b>фото</b> (не файлом).")
        return
    fid, ftype, fname = media_of(message)
    S.add_photo("extra", fid, ftype, message.from_user.id, point_id=p["id"], route_id=p["route_id"], file_name=fname)
    uid, pid = message.from_user.id, p["id"]

    async def reply():
        fresh = own_point(uid, pid)
        if fresh and fresh["status"] != "closed":
            await ask_step(message, state, fresh)

    debounce(uid, reply)


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
    db.x("DELETE FROM photos WHERE kind='extra' AND point_id=?", (p["id"],))
    await cb.answer("Фото сброшены")
    await ask_step(cb.message, state, own_point(cb.from_user.id, p["id"]))


@router.callback_query(F.data.startswith("pc:"))
async def cb_close(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    p = own_point(uid, int(cb.data[3:]))
    if not p:
        await cb.answer("Точка недоступна: рейс закрыт или маршрут снят", show_alert=True)
        return
    if p["status"] == "closed":
        await cb.answer("Точка уже закрыта")
        return
    if S.next_step(p):
        await cb.answer("Загрузите все фото перед закрытием точки", show_alert=True)
        await ask_step(cb.message, state, p)
        return
    S.close_point(p["id"], uid, "driver")
    await state.clear()
    await cb.answer("Точка закрыта ✅")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    rid = p["route_id"]
    c, t = S.route_counts(rid)
    await cb.message.answer(f"✅ Точка <b>{h(short(p['address'], 80))}</b> закрыта. Закрыто {c} из {t}.")
    if not await after_point_closed(bot, rid):
        text, markup = route_view(rid)
        await cb.message.answer(text, reply_markup=markup)


async def after_point_closed(bot: Bot, rid: int) -> bool:
    """Если все точки закрыты — маршрут закрывается автоматически, водитель получает постер благодарности."""
    if not S.auto_close_route(rid):
        return False
    r = S.route_with_wb(rid)
    c, t = S.route_counts(rid)
    if r["driver_id"] and not S.is_offline(r["driver_id"]):
        try:
            await send_poster(bot, r["driver_id"], "thanks",
                              f"🏁 Все точки маршрута №{h(r['route_no'])} (рейс {h(S.route_title(r))}) закрыты — "
                              f"маршрут закрыт автоматически.\nЗакрыто точек: {c} из {t}.\n\n"
                              f"Благодарим за отличную работу!", reply_markup=DRIVER_KB)
        except Exception:
            pass
    await notify_admins(bot, f"🏁 Маршрут №{h(r['route_no'])} (рейс {h(S.route_title(r))}) закрыт автоматически: "
                             f"все точки закрыты ({c} из {t}). Водитель: {h(S.driver_name(r['driver_id']))}.",
                        reply_markup=ikb([[("↩️ Открыть маршрут снова", f"rro:{rid}")]]))
    return True


@router.callback_query(F.data.startswith("dsh:"))
async def cb_driver_sheet(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data[4:])
    if not own_route(cb.from_user.id, rid):
        await cb.answer("Маршрут недоступен", show_alert=True)
        return
    await cb.answer()
    await send_sheet_photos(bot, cb.message.chat.id, rid)


@router.callback_query(F.data.startswith("dph:"))
async def cb_driver_point_photos(cb: CallbackQuery, bot: Bot):
    p = own_point(cb.from_user.id, int(cb.data[4:]))
    if not p:
        await cb.answer("Точка недоступна", show_alert=True)
        return
    await cb.answer()
    await send_point_photos(bot, cb.message.chat.id, p)


@router.callback_query(F.data.startswith("nav:"))
async def cb_nav(cb: CallbackQuery, bot: Bot):
    p = own_point(cb.from_user.id, int(cb.data[4:]))
    if not p:
        await cb.answer("Точка недоступна", show_alert=True)
        return
    await cb.answer("Ищу точку на карте…")
    await geo.send_nav(bot, cb.message.chat.id, p)


# ------------------------------------------------------------ close point w/o photos

@router.callback_query(F.data.startswith("pq:"))
async def cb_point_req(cb: CallbackQuery, state: FSMContext):
    p = own_point(cb.from_user.id, int(cb.data[3:]))
    if not p or p["status"] == "closed":
        await cb.answer("Точка недоступна или уже закрыта", show_alert=True)
        return
    if p["close_req_at"]:
        await cb.answer("Запрос уже отправлен, ожидайте решения логиста", show_alert=True)
        return
    await cb.answer()
    await state.set_state(PointReq.comment)
    await state.update_data(req_pid=p["id"])
    await cb.message.answer(f"📨 <b>Запрос на закрытие точки без фото</b>\n{h(short(p['address'], 80))}\n\n"
                            f"{S.photos_progress(p)}\n\n"
                            f"Напишите <b>причину</b>, почему нельзя загрузить фото (обязательно). "
                            f"Логист подтвердит или отклонит закрытие.", reply_markup=CANCEL_KB)


@router.message(PointReq.comment, F.text, NOT_MENU)
async def point_req_comment(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    text = message.text.strip()
    if len(text) < 5:
        await message.answer("Опишите причину подробнее (хотя бы несколько слов).")
        return
    pid = (await state.get_data()).get("req_pid")
    await state.clear()
    p = own_point(uid, pid)
    if not p or not S.request_point_close(pid, uid, text):
        await message.answer("Запрос уже отправлен или точка недоступна.", reply_markup=DRIVER_KB)
        return
    r = S.route_with_wb(p["route_id"])
    await message.answer("⏳ Запрос отправлен логисту. Пока ждёте решения, можно продолжать маршрут.",
                         reply_markup=DRIVER_KB)
    await notify_admins(bot, f"📨 <b>Запрос на закрытие точки без фото</b>\n"
                             f"👤 {user_label(uid)}\n"
                             f"🗺 Маршрут №{h(r['route_no'])} · рейс {h(S.route_title(r))}\n"
                             f"📍 {p['seq']}. {h(p['network'])} — {h(p['address'])}\n\n"
                             f"{S.photos_progress(p)}\n\n💬 Причина: {h(text)}",
                        reply_markup=ikb([[("✅ Подтвердить закрытие", f"pqa:{pid}")],
                                          [("❌ Отклонить", f"pqr:{pid}")]]))
    text_, markup = route_view(p["route_id"])
    await message.answer(text_, reply_markup=markup)


@router.message(PointReq.comment, NOT_MENU)
async def point_req_not_text(message: Message):
    await message.answer("Причину нужно написать текстом.")


# ------------------------------------------------------------ acks --------

@router.callback_query(F.data.startswith("iok:"))
async def cb_instr_ack(cb: CallbackQuery):
    uid = cb.from_user.id
    iid = int(cb.data[4:])
    if not db.q1("SELECT 1 FROM instructions WHERE id=?", (iid,)):
        await cb.answer("Инструкция удалена")
        return
    S.instr_ack(iid, uid)
    await cb.answer("Спасибо!")
    try:
        await cb.message.edit_reply_markup(reply_markup=ikb([[("✔️ Ознакомлен(а)", "noop_ack")]]))
    except Exception:
        pass
    left = len(S.instr_unacked(uid))
    if left == 0:
        await cb.message.answer(f"✅ Вы ознакомились со всеми инструкциями. Теперь можно нажать «{B_START}».",
                                reply_markup=DRIVER_KB)


@router.callback_query(F.data.startswith("mk:"))
async def cb_msg_ack(cb: CallbackQuery, bot: Bot):
    mid = int(cb.data[3:])
    ok = S.msg_ack(mid, cb.from_user.id)
    await cb.answer("Получение подтверждено" if ok else "Уже подтверждено")
    try:
        await cb.message.edit_reply_markup(reply_markup=ikb([[("✔️ Получено", "noop_ack")]]))
    except Exception:
        pass
    if ok:
        await acks.refresh_summary(bot, mid)


@router.callback_query(F.data == "noop_ack")
async def cb_noop_ack(cb: CallbackQuery):
    await cb.answer("Уже подтверждено")


# ------------------------------------------------------------ close route -

@router.callback_query(F.data.startswith("rc:"))
async def cb_route_close_ask(cb: CallbackQuery):
    rid = int(cb.data[3:])
    r = own_route(cb.from_user.id, rid)
    if not r:
        await cb.answer("Маршрут недоступен: рейс уже закрыт или маршрут снят", show_alert=True)
        return
    if r["close_req_at"]:
        await cb.answer("Запрос уже отправлен, ожидайте решения логиста", show_alert=True)
        return
    c, t = S.route_counts(rid)
    await cb.answer()
    text = (f"🏁 Закрыть рейс — маршрут №{h(r['route_no'])} на {S.fmt_date(r['work_date'])}?\n"
            f"Запрос уйдёт логисту на подтверждение.\n\n")
    if c < t:
        opened = db.q("SELECT seq, address FROM points WHERE route_id=? AND status!='closed' ORDER BY seq", (rid,))
        text += (f"⚠️ Не закрыто точек: <b>{t - c}</b> из {t}:\n"
                 + "\n".join(f"{p['seq']}. {h(short(p['address'], 60))}" for p in opened[:20])
                 + ("\n…" if len(opened) > 20 else "")
                 + "\n\nПосле закрытия рейса закрыть эти точки будет нельзя. Если на адресе проблема — "
                   "сначала сообщите о ней кнопкой «⚠️ Проблема на адресе».")
    else:
        text += f"Все точки закрыты: {c} из {t}."
    await cb.message.answer(text, reply_markup=ikb([[("✅ Да, отправить запрос", f"rcc:{rid}")],
                                                     [("⬅️ Вернуться к маршруту", f"r:{rid}")]]))


@router.callback_query(F.data.startswith("rcc:"))
async def cb_route_close(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    rid = int(cb.data[4:])
    r = own_route(uid, rid)
    if not r or not S.request_route_close(rid, uid):
        await cb.answer("Запрос уже отправлен или рейс закрыт", show_alert=True)
        return
    await state.clear()
    await cb.answer("Запрос отправлен")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    c, t = S.route_counts(rid)
    await cb.message.answer("⏳ Запрос на закрытие рейса отправлен логисту. Как только он подтвердит, "
                            "вы получите уведомление.", reply_markup=DRIVER_KB)
    text = (f"🏁 <b>Запрос на закрытие рейса</b>\n"
            f"👤 {user_label(uid)}\n"
            f"🗺 Маршрут №{h(r['route_no'])} ({S.fmt_date(r['work_date'])})\n"
            f"Закрыто точек: <b>{c} из {t}</b>" + (f", <b>не закрыто {t - c}</b>" if c < t else "") + ".")
    if c < t:
        opened = db.q("SELECT seq, address FROM points WHERE route_id=? AND status!='closed' ORDER BY seq", (rid,))
        text += "\n\nНе закрыты:\n" + "\n".join(f"{p['seq']}. {h(short(p['address'], 60))}" for p in opened[:20])
        text += "\n…" if len(opened) > 20 else ""
    await notify_admins(bot, text, reply_markup=ikb([[("✅ Подтвердить закрытие", f"rca:{rid}")],
                                                     [("❌ Отклонить", f"rcr:{rid}")],
                                                     [("📑 Отчёт по маршруту", f"rp:{r['workbook_id']}:{rid}")]]))


# ------------------------------------------------------------ driver note -

@router.callback_query(F.data.startswith("dn:"))
async def cb_driver_note(cb: CallbackQuery, state: FSMContext):
    p = own_point(cb.from_user.id, int(cb.data[3:]))
    if not p:
        await cb.answer("Точка недоступна: рейс закрыт или маршрут снят", show_alert=True)
        return
    await cb.answer()
    await state.set_state(NoteFlow.text)
    await state.update_data(note_point=p["id"])
    await cb.message.answer(f"📝 <b>Заметка к адресу</b> {h(short(p['address'], 80))}\n\n"
                            f"Напишите, что полезно знать водителям, которые поедут сюда в следующий раз: "
                            f"где въезд и разгрузка, часы приёмки, к кому обратиться и т.п.\n"
                            f"Заметку проверит логист, после утверждения её увидят все водители на этом адресе.",
                            reply_markup=CANCEL_KB)


@router.message(NoteFlow.text, F.text, NOT_MENU)
async def driver_note_text(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    pid = (await state.get_data()).get("note_point")
    await state.clear()
    p = own_point(uid, pid)
    if not p:
        await message.answer("Точка недоступна.", reply_markup=DRIVER_KB)
        return
    r = S.route_with_wb(p["route_id"])
    nid = N.add("point", h(message.text.strip()), uid, "driver", "pending", point_key_=N.point_key(p),
                route_no=r["route_no"])
    await message.answer("✅ Заметка отправлена логисту на проверку. После утверждения её увидят другие водители.",
                         reply_markup=DRIVER_KB)
    await notify_admins(bot, f"📝 <b>Заметка водителя на утверждение</b>\n"
                             f"👤 {user_label(uid)}\n"
                             f"📍 {h(p['network'])} — {h(p['address'])} (маршрут №{h(r['route_no'])})\n\n"
                             f"{h(message.text.strip())}",
                        reply_markup=ikb([[("✅ Утвердить", f"nok:{nid}"), ("❌ Отклонить", f"nno:{nid}")]]))


@router.message(NoteFlow.text, NOT_MENU)
async def driver_note_not_text(message: Message):
    await message.answer("Заметка принимается только текстом.")


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
            await cb.answer("Точка недоступна: рейс закрыт или маршрут снят", show_alert=True)
            return
        addr = p["address"]
        if (cb.message.text or "").startswith("📍"):  # нажато из карточки точки — показываем памятку
            await cb.message.answer(db.get_setting("problem_help", S.DEFAULT_PROBLEM_HELP))
    await state.set_state(ProblemFlow.text)
    await state.update_data(problem_point=pid)
    _problem_buf[cb.from_user.id] = {"texts": [], "files": []}
    await cb.answer()
    await cb.message.answer(f"Адрес: <b>{h(addr)}</b>\n\n"
                            f"✍️ Опишите ситуацию и прикрепите фото — можно несколькими сообщениями, "
                            f"количество фото не ограничено.\n\n{PHOTO_HINT}\n\n"
                            f"Когда всё отправите, нажмите «{B_SEND_PROBLEM}».",
                            reply_markup=PROBLEM_KB)


_problem_buf: dict[int, dict] = {}


def _buf(uid: int) -> dict:
    return _problem_buf.setdefault(uid, {"texts": [], "files": []})


@router.message(ProblemFlow.text, F.text == B_SEND_PROBLEM)
async def problem_send(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    pend = _pending.pop(uid, None)
    if pend:
        pend.cancel()
    buf = _buf(uid)
    if not buf["texts"] and not buf["files"]:
        await message.answer("Сначала напишите, что случилось, или прикрепите фото.")
        return
    data = await state.get_data()
    pid = data.get("problem_point") or None
    p = own_point(uid, pid) if pid else None
    routes = S.driver_routes(uid)
    rid = p["route_id"] if p else (routes[0]["id"] if routes else None)
    text = "\n".join(t for t in buf["texts"] if t)
    files = list(buf["files"])
    prob_id = db.x("INSERT INTO problems(point_id, route_id, driver_id, text, created_at) VALUES(?,?,?,?,?)",
                   (p["id"] if p else None, rid, uid, text, db.now_s()))
    for fid, ftype, fname in files:
        S.add_photo("problem", fid, ftype, uid, point_id=p["id"] if p else None, route_id=rid,
                    problem_id=prob_id, file_name=fname)
    _problem_buf.pop(uid, None)
    await state.clear()
    await message.answer("✅ Сообщение отправлено логисту"
                         + (f" (вложений: {len(files)})" if files else "") + ". Ожидайте ответа.",
                         reply_markup=DRIVER_KB)
    r = S.route_with_wb(rid) if rid else None
    head = (f"⚠️ <b>Проблема на адресе</b>\n"
            f"👤 {user_label(uid)}\n"
            + (f"🗺 Маршрут №{h(r['route_no'])} · рейс {h(S.route_title(r))}\n" if r else "")
            + (f"📍 {p['seq']}. {h(p['network'])} — {h(p['address'])}\n" if p else "📍 без привязки к адресу\n")
            + (f"📷 Вложений: {len(files)}\n" if files else "")
            + f"\n{h(text) if text else '(без текста)'}")
    await notify_admins(bot, head, reply_markup=ikb([[("✉️ Ответить водителю", f"ans:{uid}")]]))
    if files:
        for aid in admin_ids():
            try:
                await send_files(bot, aid, files)
            except Exception:
                pass


def point_photo_files(p) -> tuple[list, int, int]:
    """Все фото точки: обязательные, дополнительные, к проблемам. -> (файлы, доп., к проблемам)"""
    files = [(p[f], "photo", None) for f in ("photo_display", "photo_invoice", "photo_poa") if p[f]]
    extras = S.extra_photos(p["id"])
    probs = db.q("SELECT * FROM photos WHERE kind='problem' AND point_id=? ORDER BY id", (p["id"],))
    files += [(x["file_id"], x["file_type"], x["file_name"]) for x in extras]
    files += [(x["file_id"], x["file_type"], x["file_name"]) for x in probs]
    return files, len(extras), len(probs)


async def send_point_photos(bot: Bot, chat_id: int, p, empty_text: bool = True) -> bool:
    """Подпись с адресом и статусом + все фото точки. False — фото нет."""
    files, n_extra, n_prob = point_photo_files(p)
    if not files:
        if empty_text:
            await bot.send_message(chat_id, f"🖼 По точке {p['seq']}. {h(p['address'])} фото пока нет.")
        return False
    labels = [lbl for f, lbl in (("photo_display", "выкладка"), ("photo_invoice", "накладная"),
                                 ("photo_poa", "доверенность")) if p[f]]
    cap = (f"🖼 <b>Фото по точке {p['seq']}</b>. {h(p['network'] or '')} — {h(p['address'])}\n"
           f"{'✅ закрыта ' + S.fmt_time(p['closed_at']) if p['status'] == 'closed' else '⬜ не закрыта'}"
           f"{' · доверенность отсутствует' if p['poa_absent'] else ''}"
           + (f"\n📸 {', '.join(labels)}" if labels else "")
           + (f"\n📎 доп. фото: {n_extra}" if n_extra else "")
           + (f"\n⚠️ фото к проблеме: {n_prob}" if n_prob else "")
           + (f"\n💬 Закрыта без фото: {h(p['close_note'])}" if p["close_note"] else ""))
    await bot.send_message(chat_id, cap)
    await send_files(bot, chat_id, files)
    return True


async def send_sheet_photos(bot: Bot, chat_id: int, rid: int) -> bool:
    """Фото маршрутного листа маршрута. False — фото нет."""
    r = S.route_with_wb(rid)
    sheets = S.sheet_photos(rid)
    if not sheets:
        await bot.send_message(chat_id, f"📄 По маршруту №{h(r['route_no'])} ({h(S.route_title(r))}) фото маршрутного "
                                        f"листа пока нет.")
        return False
    await bot.send_message(chat_id, f"📄 <b>Фото маршрутного листа</b> — маршрут №{h(r['route_no'])} · рейс "
                                    f"{h(S.route_title(r))}\n👤 {h(S.driver_name(r['driver_id']))} · страниц: {len(sheets)}, "
                                    f"загружено {S.fmt_date(sheets[0]['created_at'][:10])} в {S.fmt_time(sheets[0]['created_at'])}")
    await send_files(bot, chat_id, [(x["file_id"], x["file_type"], x["file_name"]) for x in sheets])
    return True


async def send_files(bot: Bot, chat_id: int, files: list[tuple[str, str, str | None]]) -> None:
    """Фото и видео — альбомами по 10, документы — альбомами документов по 10."""
    visual = [f for f in files if f[1] in ("photo", "video")]
    docs = [f for f in files if f[1] not in ("photo", "video")]
    for i in range(0, len(visual), 10):
        chunk = visual[i:i + 10]
        if len(chunk) == 1:
            fid, ftype, _ = chunk[0]
            await (bot.send_photo if ftype == "photo" else bot.send_video)(chat_id, fid)
        else:
            await bot.send_media_group(chat_id, [InputMediaPhoto(media=f) if t == "photo" else InputMediaVideo(media=f)
                                                 for f, t, _ in chunk])
        await asyncio.sleep(0.3)
    for i in range(0, len(docs), 10):
        chunk = docs[i:i + 10]
        if len(chunk) == 1:
            await bot.send_document(chat_id, chunk[0][0])
        else:
            await bot.send_media_group(chat_id, [InputMediaDocument(media=f) for f, _, _ in chunk])
        await asyncio.sleep(0.3)


@router.message(ProblemFlow.text, NOT_MENU)
async def problem_collect(message: Message, state: FSMContext):
    uid = message.from_user.id
    buf = _buf(uid)
    media = media_of(message)
    txt = message.text or message.caption
    if txt:
        buf["texts"].append(txt)
    if media:
        buf["files"].append(media)
    if not txt and not media:
        await message.answer("Отправьте текст, фото, видео или файл.")
        return

    async def reply():
        b = _buf(uid)
        await message.answer(f"📥 Принято: текст — {len(b['texts'])} сообщ., вложений — {len(b['files'])}.\n"
                             f"Можно добавить ещё или нажмите «{B_SEND_PROBLEM}».", reply_markup=PROBLEM_KB)

    debounce(uid, reply)


# ------------------------------------------------------------ instructions

@router.message(F.text == B_INSTR)
async def instructions(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    rows = db.q("SELECT id, title FROM instructions ORDER BY id")
    unacked = {r["id"] for r in S.instr_unacked(uid)}
    head = (f"📖 <b>Инструкции</b>\nВсего: <b>{len(rows)}</b> · не ознакомлены: <b>{len(unacked)}</b>"
            + ("\n\n🆕 — нужно ознакомиться и нажать «✅ Ознакомлен(а)»." if unacked
               else "\n\n✅ Со всеми ознакомлены." if rows else ""))
    btns = [[(guide.TITLES["driver"], "gd:driver")]]
    btns += [[(("🆕 " if r["id"] in unacked else "✅ ") + short(r["title"], 56), f"i:{r['id']}")]
             for r in sorted(rows, key=lambda r: r["id"] not in unacked)]
    await message.answer(head, reply_markup=ikb(btns))


async def _show(cb: CallbackQuery, text: str, markup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "gd:driver")
async def cb_guide_driver(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(guide.menu_text("driver"), reply_markup=ikb(guide.menu_rows("driver")))


@router.callback_query(F.data.startswith("gt:driver:"))
async def cb_guide_driver_topic(cb: CallbackQuery):
    await cb.answer()
    idx = int(cb.data.split(":")[2])
    if idx == -1:
        await _show(cb, guide.menu_text("driver"), ikb(guide.menu_rows("driver")))
        return
    await _show(cb, guide.topic("driver", idx), ikb([[("⬅️ К обзору функций", "gt:driver:-1")]]))


@router.callback_query(F.data.startswith("i:"))
async def cb_instruction(cb: CallbackQuery, bot: Bot):
    r = db.q1("SELECT * FROM instructions WHERE id=?", (int(cb.data[2:]),))
    await cb.answer()
    if not r:
        await cb.message.answer("Инструкция удалена.")
        return
    await acks.send_instruction(bot, cb.from_user.id, r, ask_ack=not S.instr_is_acked(r["id"], cb.from_user.id))


@router.message()
async def fallback(message: Message):
    await message.answer("Выберите действие в меню 👇", reply_markup=DRIVER_KB)
