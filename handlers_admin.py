"""Сценарии администратора (логиста)."""
import asyncio
from datetime import date
from html import escape as h
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

import db
import services as S
from common import extract_content, is_admin, send_content, user_label
from config import WB_DIR
from keyboards import (A_ASSIGN, A_DRIVERS, A_EXPORT, A_INSTR, A_MSG, A_PROBLEM_TEXT, A_REPORT, A_STATS,
                       A_UPLOAD, ADMIN_BUTTONS, ADMIN_KB, CANCEL, CANCEL_KB, ikb, short)

NOT_MENU = ~F.text.in_(ADMIN_BUTTONS | {CANCEL})


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return is_admin(event.from_user.id)


router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Adm(StatesGroup):
    upload_date = State()
    instr_title = State()
    instr_body = State()
    msg = State()
    problem_text = State()


HELP = (
    "<b>Справка администратора</b>\n\n"
    "📥 <b>Загрузка Excel</b> — отправьте файл .xlsx в чат. Дата берётся из имени файла "
    "(например «04.11.2026_маршруты.xlsx») или из подписи к файлу. Каждый лист = номер маршрута. "
    "Повторная загрузка файла на ту же дату обновит адреса и сохранит уже закрытые точки и назначения.\n\n"
    "🧭 <b>Назначить маршруты</b> — выбор даты → маршрута → водителя.\n"
    "📊 <b>Статистика</b> — /stats, /stats 04.11.2026, /stats 1 6 14, /stats 04.11.2026 1 6\n"
    "📑 <b>Отчёт</b> — /report 04.11.2026 14 (дата и номер маршрута) или через кнопку.\n"
    "📤 <b>Выгрузить Excel</b> — /export или /export 04.11.2026. В столбец «кол-во ТК» ставится 1 (точка закрыта) "
    "или 0 (не закрыта), формат файла не меняется.\n"
    "📖 <b>Инструкции</b> — добавить / изменить / удалить инструкции для водителей.\n"
    "✉️ <b>Сообщение водителям</b> — инструкция всем водителям на маршруте, всем водителям или конкретному.\n"
    "⚙️ <b>Текст «Проблема»</b> — памятка и контакты, которые видит водитель по кнопке «Проблема на адресе».\n"
    "/cancel — отменить текущее действие."
)


async def safe_edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)


def date_picker(action: str):
    wbs = S.workbooks(10)
    return ikb([[(S.fmt_date(w["work_date"]), f"wd:{action}:{w['id']}")] for w in wbs]), wbs


async def choose_date_or_run(message: Message, action: str, runner):
    markup, wbs = date_picker(action)
    if not wbs:
        await message.answer("Файлы с маршрутами ещё не загружены. Отправьте Excel в чат.")
    elif len(wbs) == 1:
        await runner(message, wbs[0])
    else:
        await message.answer("Выберите дату:", reply_markup=markup)


# ------------------------------------------------------------ common ------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 Добро пожаловать, администратор!\n"
                         "Загрузите Excel с маршрутами (просто отправьте файл в чат) или выберите действие в меню.\n"
                         "Справка: /help", reply_markup=ADMIN_KB)


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code> (администратор)")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP, reply_markup=ADMIN_KB)


@router.message(Command("cancel"))
@router.message(F.text == CANCEL)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=ADMIN_KB)


# ------------------------------------------------------------ upload ------

@router.message(F.text == A_UPLOAD)
async def upload_hint(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отправьте файл Excel (.xlsx) с маршрутами.\n"
                         "Дата берётся из имени файла (например <i>04.11.2026_….xlsx</i>) "
                         "или укажите её в подписи к файлу.")


@router.message(F.document)
async def upload_file(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = doc.file_name or "routes.xlsx"
    if not name.lower().endswith((".xlsx", ".xlsm")):
        if await state.get_state() in (Adm.instr_body.state, Adm.msg.state):
            return await route_state_doc(message, state, bot)
        await message.answer("Нужен файл Excel в формате .xlsx")
        return
    if await state.get_state() in (Adm.instr_body.state, Adm.msg.state):
        return await route_state_doc(message, state, bot)
    await state.clear()
    d = S.parse_date(message.caption) or S.parse_date(name)
    tmp = WB_DIR / f"upload_{message.message_id}_{Path(name).name}"
    await bot.download(doc, destination=tmp)
    if not d:
        await state.set_state(Adm.upload_date)
        await state.update_data(tmp=str(tmp), name=name)
        await message.answer("Не удалось определить дату из имени файла. Введите дату маршрутов (ДД.ММ.ГГГГ):",
                             reply_markup=CANCEL_KB)
        return
    await do_import(message, tmp, name, d)


async def route_state_doc(message: Message, state: FSMContext, bot: Bot):
    st = await state.get_state()
    if st == Adm.instr_body.state:
        await instr_body(message, state)
    elif st == Adm.msg.state:
        await msg_send(message, state, bot)


@router.message(Adm.upload_date, F.text, NOT_MENU)
async def upload_date(message: Message, state: FSMContext):
    d = S.parse_date(message.text)
    if not d:
        await message.answer("Формат даты: ДД.ММ.ГГГГ, например 04.11.2026")
        return
    data = await state.get_data()
    await state.clear()
    await do_import(message, Path(data["tmp"]), data["name"], d)


async def do_import(message: Message, tmp: Path, name: str, d: date):
    final = WB_DIR / f"{d.isoformat()}__{Path(name).name}"
    try:
        tmp.replace(final)
        res = S.import_workbook(final, Path(name).name, d, message.from_user.id)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ Не удалось прочитать файл: {h(str(e))}", reply_markup=ADMIN_KB)
        return
    routes = ", ".join(f"№{h(no)} ({n})" for no, n in res["routes"])
    text = (f"✅ Файл на <b>{S.fmt_date(res['date'])}</b> загружен"
            + (" (заменён предыдущий)" if res["replaced"] else "") + ".\n"
            f"Маршрутов: <b>{len(res['routes'])}</b>, точек: <b>{res['points']}</b>.\n"
            f"Маршруты (точек): {routes}")
    if res["replaced"]:
        text += f"\nСохранено закрытых точек: {res['kept_closed']}"
    wb = S.workbook_by_date(d)
    await message.answer(text, reply_markup=ikb([[("🧭 Назначить маршруты", f"wd:as:{wb['id']}")]]))
    await message.answer("Меню:", reply_markup=ADMIN_KB)


# ------------------------------------------------------------ date picker -

@router.callback_query(F.data.startswith("wd:"))
async def cb_date(cb: CallbackQuery):
    _, act, wb_id = cb.data.split(":")
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (int(wb_id),))
    await cb.answer()
    if not wb:
        await cb.message.answer("Файл не найден.")
        return
    runner = {"as": show_assign, "st": show_stats, "ex": send_export, "rp": show_report_routes}[act]
    await runner(cb.message, wb)


# ------------------------------------------------------------ assign ------

@router.message(F.text == A_ASSIGN)
async def assign_menu(message: Message, state: FSMContext):
    await state.clear()
    await choose_date_or_run(message, "as", show_assign)


async def show_assign(message: Message, wb, edit_cb: CallbackQuery | None = None):
    rows = []
    for r in S.routes_of(wb["id"]):
        c, t = S.route_counts(r["id"])
        rows.append([(f"№{r['route_no']} ({c}/{t}) — {short(S.driver_name(r['driver_id']), 28)}", f"ar:{r['id']}")])
    text = f"🧭 <b>Назначение маршрутов на {S.fmt_date(wb['work_date'])}</b>\nВыберите маршрут:"
    if edit_cb:
        await safe_edit(edit_cb, text, ikb(rows))
    else:
        await message.answer(text, reply_markup=ikb(rows))


@router.callback_query(F.data.startswith("ar:"))
async def cb_assign_route(cb: CallbackQuery):
    rid = int(cb.data[3:])
    r = S.route_with_wb(rid)
    drv = S.drivers()
    if not drv:
        await cb.answer("Нет зарегистрированных водителей. Попросите водителей нажать /start в боте.",
                        show_alert=True)
        return
    busy = {x["driver_id"]: x["route_no"] for x in db.q("SELECT driver_id, route_no FROM routes "
                                                       "WHERE workbook_id=? AND driver_id IS NOT NULL",
                                                       (r["workbook_id"],))}
    rows = []
    for d in drv:
        mark = "✔️ " if d["tg_id"] == r["driver_id"] else ""
        extra = f" [№{busy[d['tg_id']]}]" if d["tg_id"] in busy and d["tg_id"] != r["driver_id"] else ""
        rows.append([(f"{mark}{short(d['full_name'], 40)}{extra}", f"ad:{rid}:{d['tg_id']}")])
    rows.append([("🚫 Снять назначение", f"ad:{rid}:0")])
    rows.append([("⬅️ Назад", f"wd:as:{r['workbook_id']}")])
    await cb.answer()
    await safe_edit(cb, f"Маршрут №{h(r['route_no'])} ({S.fmt_date(r['work_date'])}).\n"
                        f"Сейчас: {h(S.driver_name(r['driver_id']))}\nВыберите водителя:", ikb(rows))


@router.callback_query(F.data.startswith("ad:"))
async def cb_assign_driver(cb: CallbackQuery, bot: Bot):
    _, rid, uid = cb.data.split(":")
    rid, uid = int(rid), int(uid)
    r = S.route_with_wb(rid)
    prev = r["driver_id"]
    db.x("UPDATE routes SET driver_id=?, assigned_at=? WHERE id=?", (uid or None, db.now_s(), rid))
    await cb.answer("Сохранено")
    if uid and uid != prev:
        c, t = S.route_counts(rid)
        try:
            await bot.send_message(uid, f"🗺 Вам назначен маршрут <b>№{h(r['route_no'])}</b> на "
                                        f"<b>{S.fmt_date(r['work_date'])}</b>, точек: {t}.\n"
                                        f"Нажмите «🚚 Начать маршрут», когда будете готовы.")
        except Exception:
            await cb.message.answer("⚠️ Не удалось уведомить водителя (он не запускал бота?).")
    if prev and prev != uid:
        try:
            await bot.send_message(prev, f"ℹ️ Маршрут №{h(r['route_no'])} на {S.fmt_date(r['work_date'])} "
                                         f"с вас снят.")
        except Exception:
            pass
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (r["workbook_id"],))
    await show_assign(cb.message, wb, edit_cb=cb)


# ------------------------------------------------------------ stats -------

@router.message(F.text == A_STATS)
async def stats_btn(message: Message, state: FSMContext):
    await state.clear()
    await choose_date_or_run(message, "st", show_stats)


async def show_stats(message: Message, wb, route_nos=None):
    await send_content(message.bot, message.chat.id, S.stats_text(wb, route_nos))


def split_date_args(args: str | None):
    parts = (args or "").replace(",", " ").split()
    d = None
    rest = []
    for p in parts:
        pd = S.parse_date(p) if "." in p or "/" in p or "-" in p else None
        if pd and not d:
            d = pd
        else:
            rest.append(p)
    return d, rest


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    d, routes = split_date_args(command.args)
    wb = S.workbook_by_date(d) if d else S.latest_workbook()
    if not wb:
        await message.answer("Нет загруженного файла" + (f" на {d:%d.%m.%Y}" if d else "") + ".")
        return
    await show_stats(message, wb, routes or None)


# ------------------------------------------------------------ report ------

@router.message(F.text == A_REPORT)
async def report_btn(message: Message, state: FSMContext):
    await state.clear()
    await choose_date_or_run(message, "rp", show_report_routes)


async def show_report_routes(message: Message, wb):
    rows = [[("📋 Все маршруты", f"rp:{wb['id']}:0")]]
    btns = [(f"№{r['route_no']}", f"rp:{wb['id']}:{r['id']}") for r in S.routes_of(wb["id"])]
    rows += [btns[i:i + 5] for i in range(0, len(btns), 5)]
    await message.answer(f"📑 Отчёт на {S.fmt_date(wb['work_date'])}. Выберите маршрут:", reply_markup=ikb(rows))


@router.callback_query(F.data.startswith("rp:"))
async def cb_report(cb: CallbackQuery):
    _, wb_id, rid = cb.data.split(":")
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (int(wb_id),))
    await cb.answer()
    if int(rid) == 0:
        await send_content(cb.bot, cb.message.chat.id, S.report_all_text(wb))
        return
    r = db.q1("SELECT * FROM routes WHERE id=?", (int(rid),))
    await send_content(cb.bot, cb.message.chat.id, S.report_text(wb, r),
                       reply_markup=ikb([[("🖼 Фото по маршруту", f"ph:{r['id']}")]]))


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    d, routes = split_date_args(command.args)
    wb = S.workbook_by_date(d) if d else S.latest_workbook()
    if not wb:
        await message.answer("Нет загруженного файла" + (f" на {d:%d.%m.%Y}" if d else "") + ".")
        return
    if not routes:
        await show_report_routes(message, wb)
        return
    for no in routes:
        r = db.q1("SELECT * FROM routes WHERE workbook_id=? AND lower(route_no)=lower(?)", (wb["id"], no))
        if not r:
            await message.answer(f"Маршрут №{h(no)} не найден на {S.fmt_date(wb['work_date'])}.")
            continue
        await send_content(message.bot, message.chat.id, S.report_text(wb, r),
                           reply_markup=ikb([[("🖼 Фото по маршруту", f"ph:{r['id']}")]]))


@router.callback_query(F.data.startswith("ph:"))
async def cb_photos(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data[3:])
    pts = db.q("SELECT * FROM points WHERE route_id=? AND (photo_display IS NOT NULL OR photo_invoice IS NOT NULL "
               "OR photo_poa IS NOT NULL) ORDER BY seq", (rid,))
    await cb.answer()
    if not pts:
        await cb.message.answer("По маршруту пока нет фотографий.")
        return
    for p in pts:
        cap = (f"{p['seq']}. {p['network']} — {p['address']}\n"
               f"{'✅ закрыта ' + S.fmt_time(p['closed_at']) if p['status'] == 'closed' else '⬜ не закрыта'}"
               f"{' · доверенность отсутствует' if p['poa_absent'] else ''}")
        media = []
        for f in ("photo_display", "photo_invoice", "photo_poa"):
            if p[f]:
                media.append(InputMediaPhoto(media=p[f], caption=None if media else cap[:1024]))
        try:
            await bot.send_media_group(cb.message.chat.id, media)
        except Exception as e:  # noqa: BLE001
            await cb.message.answer(f"Не удалось отправить фото точки {p['seq']}: {h(str(e))}")
        await asyncio.sleep(0.4)


# ------------------------------------------------------------ export ------

@router.message(F.text == A_EXPORT)
async def export_btn(message: Message, state: FSMContext):
    await state.clear()
    await choose_date_or_run(message, "ex", send_export)


@router.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    d, _ = split_date_args(command.args)
    wb = S.workbook_by_date(d) if d else S.latest_workbook()
    if not wb:
        await message.answer("Нет загруженного файла" + (f" на {d:%d.%m.%Y}" if d else "") + ".")
        return
    await send_export(message, wb)


async def send_export(message: Message, wb):
    try:
        path = S.export_workbook(wb["id"])
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ Ошибка выгрузки: {h(str(e))}")
        return
    tc = tt = 0
    for r in S.routes_of(wb["id"]):
        c, t = S.route_counts(r["id"])
        tc, tt = tc + c, tt + t
    await message.answer_document(FSInputFile(path, filename=wb["filename"]),
                                  caption=f"📤 Выгрузка на {S.fmt_date(wb['work_date'])} "
                                          f"(сформирована {db.now():%d.%m.%Y %H:%M}).\n"
                                          f"Закрыто точек: {tc} из {tt}.")


# ------------------------------------------------------------ instructions

def instr_list_markup():
    rows = [[(short(r["title"], 55), f"iv:{r['id']}")] for r in db.q("SELECT id, title FROM instructions ORDER BY id")]
    rows.append([("➕ Добавить инструкцию", "ia")])
    return ikb(rows)


@router.message(F.text == A_INSTR)
async def instr_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("📖 Инструкции для водителей. Нажмите на инструкцию, чтобы просмотреть, "
                         "изменить или удалить её.", reply_markup=instr_list_markup())


@router.callback_query(F.data == "ia")
async def cb_instr_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.instr_title)
    await state.update_data(instr_id=None)
    await cb.answer()
    await cb.message.answer("Введите <b>заголовок</b> инструкции:", reply_markup=CANCEL_KB)


@router.callback_query(F.data.startswith("iv:"))
async def cb_instr_view(cb: CallbackQuery, bot: Bot):
    r = db.q1("SELECT * FROM instructions WHERE id=?", (int(cb.data[3:]),))
    await cb.answer()
    if not r:
        await cb.message.answer("Инструкция не найдена.")
        return
    await send_content(bot, cb.message.chat.id, f"📖 <b>{h(r['title'])}</b>\n\n{r['body'] or ''}",
                       r["file_id"], r["file_type"],
                       reply_markup=ikb([[("✏️ Изменить", f"ie:{r['id']}"), ("🗑 Удалить", f"idl:{r['id']}")]]))


@router.callback_query(F.data.startswith("ie:"))
async def cb_instr_edit(cb: CallbackQuery, state: FSMContext):
    iid = int(cb.data[3:])
    await state.set_state(Adm.instr_title)
    await state.update_data(instr_id=iid)
    await cb.answer()
    await cb.message.answer("Введите новый <b>заголовок</b> (или «-», чтобы оставить прежний):",
                            reply_markup=CANCEL_KB)


@router.message(Adm.instr_title, F.text, NOT_MENU)
async def instr_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(Adm.instr_body)
    await message.answer("Отправьте <b>текст</b> инструкции. Можно прикрепить фото, видео или документ "
                         "(текст — в подписи).")


@router.message(Adm.instr_body, NOT_MENU)
async def instr_body(message: Message, state: FSMContext):
    text, file_id, file_type = extract_content(message)
    if not text and not file_id:
        await message.answer("Отправьте текст, фото, видео или документ.")
        return
    data = await state.get_data()
    await state.clear()
    iid, title = data.get("instr_id"), data.get("title")
    if iid:
        old = db.q1("SELECT * FROM instructions WHERE id=?", (iid,))
        if not old:
            await message.answer("Инструкция была удалена.", reply_markup=ADMIN_KB)
            return
        title = old["title"] if title == "-" else title
        db.x("UPDATE instructions SET title=?, body=?, file_id=?, file_type=?, updated_at=? WHERE id=?",
             (title, text, file_id, file_type, db.now_s(), iid))
        await message.answer("✅ Инструкция обновлена.", reply_markup=ADMIN_KB)
    else:
        if title == "-":
            title = "Инструкция"
        db.x("INSERT INTO instructions(title, body, file_id, file_type, created_at, updated_at) VALUES(?,?,?,?,?,?)",
             (title, text, file_id, file_type, db.now_s(), db.now_s()))
        await message.answer("✅ Инструкция добавлена.", reply_markup=ADMIN_KB)
    await message.answer("📖 Инструкции:", reply_markup=instr_list_markup())


@router.callback_query(F.data.startswith("idl:"))
async def cb_instr_del(cb: CallbackQuery):
    iid = int(cb.data[4:])
    await cb.answer()
    await cb.message.answer("Удалить инструкцию?", reply_markup=ikb([[("🗑 Да, удалить", f"idc:{iid}"),
                                                                      ("Отмена", "noop")]]))


@router.callback_query(F.data.startswith("idc:"))
async def cb_instr_del_confirm(cb: CallbackQuery):
    db.x("DELETE FROM instructions WHERE id=?", (int(cb.data[4:]),))
    await cb.answer("Удалено")
    await safe_edit(cb, "🗑 Инструкция удалена.\n\n📖 Инструкции:", instr_list_markup())


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer("Отменено")
    try:
        await cb.message.delete()
    except Exception:
        pass


# ------------------------------------------------------------ messages ----

def active_driver_ids() -> list[int]:
    wb = S.latest_workbook()
    if not wb:
        return []
    return [r["driver_id"] for r in db.q("SELECT DISTINCT driver_id FROM routes WHERE workbook_id=? "
                                         "AND driver_id IS NOT NULL", (wb["id"],))]


@router.message(F.text == A_MSG)
async def msg_menu(message: Message, state: FSMContext):
    await state.clear()
    rows = [[("🚚 Всем водителям на маршрутах (текущая дата)", "mt:active")],
            [("👥 Всем зарегистрированным водителям", "mt:all")]]
    act = set(active_driver_ids())
    for d in S.drivers():
        rows.append([(("🚚 " if d["tg_id"] in act else "") + short(d["full_name"], 45), f"mt:{d['tg_id']}")])
    await message.answer("✉️ Кому отправить инструкцию / сообщение?", reply_markup=ikb(rows))


@router.callback_query(F.data.startswith("mt:") | F.data.startswith("ans:"))
async def cb_msg_target(cb: CallbackQuery, state: FSMContext):
    target = cb.data.split(":", 1)[1]
    if target == "active":
        ids, label = active_driver_ids(), "всем водителям на маршрутах"
    elif target == "all":
        ids, label = [d["tg_id"] for d in S.drivers()], "всем водителям"
    else:
        ids, label = [int(target)], S.driver_name(int(target))
    if not ids:
        await cb.answer("Нет получателей", show_alert=True)
        return
    await state.set_state(Adm.msg)
    await state.update_data(targets=ids, label=label)
    await cb.answer()
    await cb.message.answer(f"Получатель: <b>{h(label)}</b> ({len(ids)}).\n"
                            f"Отправьте сообщение (текст, фото, видео, документ, голосовое):",
                            reply_markup=CANCEL_KB)


@router.message(Adm.msg, NOT_MENU)
async def msg_send(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    ok = 0
    for uid in data["targets"]:
        try:
            if message.text:
                await bot.send_message(uid, f"📨 <b>Сообщение от логиста</b>\n\n{message.html_text}")
            else:
                await bot.send_message(uid, "📨 <b>Сообщение от логиста</b>")
                await bot.copy_message(uid, message.chat.id, message.message_id)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    db.x("INSERT INTO admin_messages(admin_id, target, text, created_at) VALUES(?,?,?,?)",
         (message.from_user.id, data["label"], message.text or message.caption or "(медиа)", db.now_s()))
    await message.answer(f"✅ Доставлено: {ok} из {len(data['targets'])}.", reply_markup=ADMIN_KB)


# ------------------------------------------------------------ drivers -----

@router.message(F.text == A_DRIVERS)
async def drivers_menu(message: Message, state: FSMContext):
    await state.clear()
    drv = S.drivers()
    if not drv:
        await message.answer("Водителей пока нет. Водитель регистрируется сам: открывает бота и нажимает /start.")
        return
    act = {}
    wb = S.latest_workbook()
    if wb:
        for r in db.q("SELECT driver_id, route_no FROM routes WHERE workbook_id=? AND driver_id IS NOT NULL",
                      (wb["id"],)):
            act.setdefault(r["driver_id"], []).append(r["route_no"])
    lines = [f"👥 <b>Водители ({len(drv)})</b>" + (f" · маршруты на {S.fmt_date(wb['work_date'])}" if wb else ""), ""]
    for d in drv:
        lines.append(f"• {user_label(d['tg_id'])} — "
                     + (f"№{', №'.join(act[d['tg_id']])}" if d["tg_id"] in act else "без маршрута"))
    rows = [[(f"🗑 {short(d['full_name'], 45)}", f"dd:{d['tg_id']}")] for d in drv]
    await send_content(message.bot, message.chat.id, "\n".join(lines))
    await message.answer("Чтобы удалить водителя, нажмите на него:", reply_markup=ikb(rows))


@router.callback_query(F.data.startswith("dd:"))
async def cb_driver_del(cb: CallbackQuery):
    uid = int(cb.data[3:])
    await cb.answer()
    await cb.message.answer(f"Удалить водителя {user_label(uid)}? Назначенные ему маршруты будут сняты.",
                            reply_markup=ikb([[("🗑 Да", f"ddc:{uid}"), ("Отмена", "noop")]]))


@router.callback_query(F.data.startswith("ddc:"))
async def cb_driver_del_confirm(cb: CallbackQuery):
    uid = int(cb.data[4:])
    db.x("UPDATE users SET active=0 WHERE tg_id=?", (uid,))
    db.x("UPDATE routes SET driver_id=NULL WHERE driver_id=? AND id IN (SELECT r.id FROM routes r JOIN points p "
         "ON p.route_id=r.id GROUP BY r.id HAVING SUM(p.status!='closed')>0)", (uid,))
    await cb.answer("Удалён")
    await safe_edit(cb, "🗑 Водитель удалён. Для повторной регистрации ему нужно нажать /start.")


# ------------------------------------------------------------ problem text

@router.message(F.text == A_PROBLEM_TEXT)
async def problem_text_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Текущая памятка, которую видит водитель по кнопке «⚠️ Проблема на адресе»:")
    await message.answer(db.get_setting("problem_help", S.DEFAULT_PROBLEM_HELP),
                         reply_markup=ikb([[("✏️ Изменить", "pe")]]))


@router.callback_query(F.data == "pe")
async def cb_problem_edit(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.problem_text)
    await cb.answer()
    await cb.message.answer("Отправьте новый текст памятки: порядок действий и контакты (телефоны, ФИО).",
                            reply_markup=CANCEL_KB)


@router.message(Adm.problem_text, F.text, NOT_MENU)
async def problem_text_save(message: Message, state: FSMContext):
    db.set_setting("problem_help", message.html_text)
    await state.clear()
    await message.answer("✅ Памятка обновлена.", reply_markup=ADMIN_KB)


@router.message()
async def admin_fallback(message: Message):
    await message.answer("Выберите действие в меню 👇 (/help — справка)", reply_markup=ADMIN_KB)
