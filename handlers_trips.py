"""Администратор: рейсы (названия, закрытие), маршруты и точки (закрытие, фото за водителя),
запросы водителей на закрытие точки без фото, архив ZIP (админ и заказчик)."""
from __future__ import annotations

import asyncio
from html import escape as h

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import backup
import db
import geo
import notes as N
import services as S
from common import is_admin, send_poster, user_label
from handlers_admin import ADM, IsStaff, NOT_MENU, safe_edit, zip_send
from keyboards import A_CLOSE, A_TRIPS, A_ZIP, A_ZIP_OLD, ADMIN_KB, CANCEL_KB, ikb, short

router = Router(name="trips")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())


class Trip(StatesGroup):
    rename = State()
    geo = State()            # админ указывает точку на карте (геопозиция)
    photo = State()          # админ загружает фото в точку
    close_reason = State()   # причина закрытия точки без фото


SLOTS = (("photo_display", "выкладка"), ("photo_invoice", "накладная"), ("photo_poa", "доверенность"))
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


# ------------------------------------------------------------ рейсы ------

@router.message(F.text.in_({A_TRIPS, A_CLOSE}), ADM)
async def trips_menu(message: Message, state: FSMContext):
    await state.clear()
    wbs = S.workbooks(15)
    if not wbs:
        await message.answer("Рейсов пока нет. Загрузите Excel.")
        return
    rows = []
    for w in wbs:
        c, t = S.wb_totals(w["id"])
        rows.append([(f"{'🔒' if w['closed_at'] else '🚚'} {S.wb_title(w)} · {c}/{t}", f"tc:{w['id']}")])
    await message.answer("🗂 <b>Рейсы</b> (🚚 открыт, 🔒 закрыт · закрыто точек / всего). Выберите рейс:",
                         reply_markup=ikb(rows))


def trip_card(wb):
    c, t = S.wb_totals(wb["id"])
    routes = S.routes_of(wb["id"])
    closed_routes = sum(1 for r in routes if r["closed_at"])
    text = (f"🗂 <b>Рейс {h(S.wb_title(wb))}</b>\n"
            f"Файл от {S.fmt_date(wb['work_date'])}: {h(wb['filename'])}\n"
            f"Статус: {'🔒 закрыт ' + wb['closed_at'][:16] if wb['closed_at'] else '🚚 открыт'}\n"
            f"Маршрутов: {len(routes)}, закрыто маршрутов: {closed_routes}\n"
            f"Точек закрыто: <b>{c} из {t}</b>")
    rows = [[("🛣 Маршруты и точки", f"tr:{wb['id']}")],
            [("✏️ Переименовать рейс", f"tn:{wb['id']}")]]
    if wb["closed_at"]:
        rows.append([("↩️ Открыть рейс снова", f"ro:{wb['id']}")])
        rows.append([("🗑 Удалить рейс", f"td:{wb['id']}")])
    else:
        rows.append([("🔒 Закрыть рейс", f"cl:{wb['id']}")])
    rows.append([("🗜 ZIP рейса", f"zq2:{wb['id']}")])
    return text, ikb(rows)


@router.callback_query(F.data.startswith("tc:"), ADM)
async def cb_trip(cb: CallbackQuery):
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (int(cb.data[3:]),))
    await cb.answer()
    text, markup = trip_card(wb)
    await cb.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("tn:"), ADM)
async def cb_trip_rename(cb: CallbackQuery, state: FSMContext):
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (int(cb.data[3:]),))
    await cb.answer()
    await state.set_state(Trip.rename)
    await state.update_data(wb_id=wb["id"])
    await cb.message.answer(f"Текущее название: <b>{h(S.wb_title(wb))}</b>\n"
                            f"Введите новое название рейса (например, «09-15 ноябрь 2026»):", reply_markup=CANCEL_KB)


@router.message(Trip.rename, F.text, NOT_MENU, ADM)
async def trip_rename_save(message: Message, state: FSMContext):
    wb_id = (await state.get_data()).get("wb_id")
    await state.clear()
    title = " ".join(message.text.split())[:60]
    db.x("UPDATE workbooks SET title=? WHERE id=?", (title, wb_id))
    await message.answer(f"✅ Рейс переименован: <b>{h(title)}</b>", reply_markup=ADMIN_KB)


# ------------------------------------------------------------ маршруты ---

@router.callback_query(F.data.startswith("tr:"), ADM)
async def cb_trip_routes(cb: CallbackQuery):
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (int(cb.data[3:]),))
    await cb.answer()
    rows = []
    for r in S.routes_of(wb["id"]):
        c, t = S.route_counts(r["id"])
        icon = "🏁" if r["closed_at"] else ("🚚" if r["started_at"] or c else "⏸")
        rows.append([(f"{icon} №{r['route_no']} · {c}/{t} · {short(S.driver_name(r['driver_id']), 22)}",
                      f"rt:{r['id']}")])
    await cb.message.answer(f"🛣 Маршруты рейса <b>{h(S.wb_title(wb))}</b>\n🏁 закрыт · 🚚 в работе · ⏸ не начат",
                            reply_markup=ikb(rows))


def route_card(rid: int):
    r = S.route_with_wb(rid)
    c, t = S.route_counts(rid)
    sheets = len(S.sheet_photos(rid))
    reqs = db.q1("SELECT COUNT(*) n FROM points WHERE route_id=? AND close_req_at IS NOT NULL AND status!='closed'",
                 (rid,))["n"]
    text = (f"🛣 <b>Маршрут №{h(r['route_no'])}</b> · рейс {h(S.route_title(r))}\n"
            f"👤 {h(S.driver_name(r['driver_id']))}\n"
            f"Статус: {S.route_state(r)}\n"
            f"Точек закрыто: <b>{c} из {t}</b>\n"
            f"📄 Фото маршрутного листа: {sheets or 'нет'}"
            + (f"\n⏳ Запросов на закрытие точек без фото: <b>{reqs}</b>" if reqs else ""))
    rows = [[("📍 Точки маршрута", f"rpts:{rid}")]]
    if r["closed_at"]:
        rows.append([("↩️ Открыть маршрут снова", f"rro:{rid}")])
    elif not r["wb_closed_at"]:
        rows.append([("🔒 Закрыть маршрут", f"rcl:{rid}")])
    rows.append([("🖼 Фото по маршруту", f"ph:{rid}"), ("📝 Примечания", f"ntr:{rid}")])
    return text, ikb(rows)


@router.callback_query(F.data.startswith("rt:"), ADM)
async def cb_route_card(cb: CallbackQuery):
    await cb.answer()
    text, markup = route_card(int(cb.data[3:]))
    await cb.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("rcl:"), ADM)
async def cb_route_close_ask(cb: CallbackQuery):
    rid = int(cb.data[4:])
    r = S.route_with_wb(rid)
    c, t = S.route_counts(rid)
    await cb.answer()
    await cb.message.answer(f"Закрыть маршрут №{h(r['route_no'])} (рейс {h(S.route_title(r))})?\n"
                            f"Закрыто точек: {c} из {t}" + (f", незакрытые ({t - c}) получат 0 в Excel." if c < t else "."),
                            reply_markup=ikb([[("✅ Закрыть маршрут", f"rclc:{rid}")], [("Отмена", "noop")]]))


@router.callback_query(F.data.startswith("rclc:"), ADM)
async def cb_route_close(cb: CallbackQuery, bot: Bot):
    rid = int(cb.data[5:])
    if not S.admin_close_route(rid, cb.from_user.id):
        await cb.answer("Маршрут уже закрыт", show_alert=True)
        return
    await cb.answer("Маршрут закрыт")
    r = S.route_with_wb(rid)
    c, t = S.route_counts(rid)
    await safe_edit(cb, f"🏁 Маршрут №{h(r['route_no'])} закрыт. Закрыто точек: {c} из {t}.",
                    ikb([[("↩️ Открыть маршрут снова", f"rro:{rid}")]]))
    if r["driver_id"]:
        try:
            await send_poster(bot, r["driver_id"], "thanks",
                              f"🏁 Логист закрыл маршрут №{h(r['route_no'])} (рейс {h(S.route_title(r))}).\n"
                              f"Закрыто точек: {c} из {t}.\n\nБлагодарим за отличную работу!")
        except Exception:
            pass


# ------------------------------------------------------------ точки ------

def point_icon(p) -> str:
    if p["status"] == "closed":
        return "⚠️" if p["close_note"] else "✅"
    return "⏳" if p["close_req_at"] else "⬜"


@router.callback_query(F.data.startswith("rpts:"), ADM)
async def cb_points(cb: CallbackQuery):
    rid = int(cb.data[5:])
    await cb.answer()
    pts = db.q("SELECT * FROM points WHERE route_id=? ORDER BY seq", (rid,))
    rows = [[(f"{point_icon(p)} {p['seq']}. {short(p['address'], 44)}", f"apc:{p['id']}")] for p in pts]
    await cb.message.answer("📍 Точки маршрута\n✅ закрыта · ⚠️ закрыта без фото · ⏳ запрос на закрытие · ⬜ открыта",
                            reply_markup=ikb(rows[:95]))


def admin_point_card(pid: int):
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    r = S.route_with_wb(p["route_id"])
    text = S.point_text(p, r)
    if p["status"] == "closed":
        how = {"admin": "логистом", "approved": "по запросу водителя (подтверждено)", "driver": "водителем"}.get(
            p["closed_via"] or "driver", "")
        text += f"\nЗакрыта {how}: {h(S.driver_name(p['closed_by']) if p['closed_by'] else '—')}"
    if p["close_note"]:
        text += f"\n💬 Причина закрытия без фото: {h(p['close_note'])}"
    if p["close_req_at"] and p["status"] != "closed":
        text += (f"\n\n⏳ <b>Водитель просит закрыть точку без фото</b> ({S.fmt_time(p['close_req_at'])}):\n"
                 f"💬 {h(p['close_req_comment'] or '')}")
    rows = []
    open_ = p["status"] != "closed" and not r["closed_at"] and not r["wb_closed_at"]
    if p["close_req_at"] and p["status"] != "closed":
        rows.append([("✅ Подтвердить закрытие", f"pqa:{pid}"), ("❌ Отклонить", f"pqr:{pid}")])
    if open_:
        rows.append([("📷 Добавить фото", f"apf:{pid}")])
        rows.append([("✅ Закрыть точку", f"apx:{pid}")])
    elif p["status"] == "closed":
        rows.append([("↩️ Открыть точку снова", f"apo:{pid}")])
    c = geo.cached(p)
    text += ("\n🧭 Точка на карте: указана логистом" if c and c[2] == "admin" else
             "\n🧭 Точка на карте: найдена автоматически по адресу (проверьте кнопкой «Маршрут»)" if c else
             "\n🧭 Точка на карте: ещё не найдена")
    rows.append([("🧭 Маршрут до точки", f"nav:{pid}"), ("📍 Указать на карте", f"agl:{pid}")])
    rows.append([("⬅️ К точкам маршрута", f"rpts:{p['route_id']}")])
    return text, ikb(rows)


@router.callback_query(F.data.startswith("apc:"), ADM)
async def cb_admin_point(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    text, markup = admin_point_card(int(cb.data[4:]))
    await cb.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("apf:"), ADM)
async def cb_admin_photo(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data[4:])
    await cb.answer()
    await state.set_state(Trip.photo)
    await state.update_data(ap_pid=pid)
    await cb.message.answer("📷 Отправьте фото для точки. Бот разложит их по порядку: 1) выкладка, "
                            "2) накладная, 3) доверенность; следующие — дополнительные фото. Можно альбомом.",
                            reply_markup=ikb([[("🚫 Доверенность отсутствует", f"apn:{pid}")],
                                              [("✅ Готово", f"apc:{pid}")]]))


@router.message(Trip.photo, F.photo | F.document, ADM)
async def admin_got_photo(message: Message, state: FSMContext):
    pid = (await state.get_data()).get("ap_pid")
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    if not p:
        await state.clear()
        return
    fid = message.photo[-1].file_id if message.photo else message.document.file_id
    ftype = "photo" if message.photo else "document"
    step = S.next_step(p)
    if step and ftype == "photo":
        field = {"display": "photo_display", "invoice": "photo_invoice", "poa": "photo_poa"}[step]
        db.x(f"UPDATE points SET {field}=? WHERE id=?", (fid, pid))
    else:
        S.add_photo("extra", fid, ftype, message.from_user.id, point_id=pid, route_id=p["route_id"],
                    file_name=message.document.file_name if message.document else None)

    async def reply():
        fresh = db.q1("SELECT * FROM points WHERE id=?", (pid,))
        rows = []
        if S.next_step(fresh) == "poa":
            rows.append([("🚫 Доверенность отсутствует", f"apn:{pid}")])
        rows += [[("✅ Закрыть точку", f"apx:{pid}")], [("⬅️ Карточка точки", f"apc:{pid}")]]
        await message.answer(f"📷 Фото сохранены.\n\n{S.photos_progress(fresh)}\n\nМожно отправить ещё или:",
                             reply_markup=ikb(rows))

    debounce(message.from_user.id, reply)


@router.callback_query(F.data.startswith("apn:"), ADM)
async def cb_admin_no_poa(cb: CallbackQuery):
    pid = int(cb.data[4:])
    db.x("UPDATE points SET poa_absent=1, photo_poa=NULL WHERE id=?", (pid,))
    await cb.answer("Отмечено: доверенность отсутствует")
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    await cb.message.answer(S.photos_progress(p), reply_markup=ikb([[("✅ Закрыть точку", f"apx:{pid}")],
                                                                    [("⬅️ Карточка точки", f"apc:{pid}")]]))


async def _finish_close(bot: Bot, message: Message, pid: int, admin_id: int, via: str, note: str | None):
    from handlers_driver import after_point_closed
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    if not S.close_point(pid, admin_id, via, note):
        await message.answer("Точка уже закрыта.", reply_markup=ADMIN_KB)
        return
    r = S.route_with_wb(p["route_id"])
    c, t = S.route_counts(r["id"])
    await message.answer(f"✅ Точка {p['seq']}. {h(p['address'])} закрыта"
                         + (f" (без фото: {h(note)})" if note else "") + f". По маршруту: {c} из {t}.",
                         reply_markup=ADMIN_KB)
    if r["driver_id"]:
        try:
            await bot.send_message(r["driver_id"], f"✅ Логист закрыл точку {p['seq']}. {h(p['address'])} "
                                                   f"(маршрут №{h(r['route_no'])}).")
        except Exception:
            pass
    await after_point_closed(bot, r["id"])


@router.callback_query(F.data.startswith("apx:"), ADM)
async def cb_admin_close_point(cb: CallbackQuery, state: FSMContext, bot: Bot):
    pid = int(cb.data[4:])
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    await cb.answer()
    if p["status"] == "closed":
        await cb.message.answer("Точка уже закрыта.")
        return
    if S.photos_complete(p):
        await state.clear()
        await _finish_close(bot, cb.message, pid, cb.from_user.id, "admin", None)
        return
    await state.set_state(Trip.close_reason)
    await state.update_data(cr_pid=pid)
    await cb.message.answer(f"{S.photos_progress(p)}\n\n⚠️ Не все фото загружены. Укажите <b>причину</b> закрытия "
                            f"точки без фото (обязательно):", reply_markup=CANCEL_KB)


@router.message(Trip.close_reason, F.text, NOT_MENU, ADM)
async def admin_close_reason(message: Message, state: FSMContext, bot: Bot):
    if len(message.text.strip()) < 3:
        await message.answer("Укажите причину текстом.")
        return
    pid = (await state.get_data()).get("cr_pid")
    await state.clear()
    await _finish_close(bot, message, pid, message.from_user.id, "admin", message.text.strip())


@router.callback_query(F.data.startswith("apo:"), ADM)
async def cb_admin_reopen_point(cb: CallbackQuery):
    pid = int(cb.data[4:])
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    r = S.route_with_wb(p["route_id"])
    if r["wb_closed_at"]:
        await cb.answer("Рейс закрыт. Сначала откройте рейс снова.", show_alert=True)
        return
    S.reopen_point(pid)
    if r["closed_at"]:
        S.reopen_route(r["id"])
    await cb.answer("Точка открыта")
    text, markup = admin_point_card(pid)
    await cb.message.answer(("↩️ Точка снова открыта" + (", маршрут тоже открыт" if r["closed_at"] else "") + ".\n\n")
                            + text, reply_markup=markup)


@router.callback_query(F.data.startswith("nav:"), ADM)
async def cb_admin_nav(cb: CallbackQuery, bot: Bot):
    p = db.q1("SELECT * FROM points WHERE id=?", (int(cb.data[4:]),))
    await cb.answer("Ищу точку на карте…")
    await geo.send_nav(bot, cb.message.chat.id, p)


@router.callback_query(F.data.startswith("agl:"), ADM)
async def cb_admin_geo(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data[4:])
    await cb.answer()
    await state.set_state(Trip.geo)
    await state.update_data(geo_pid=pid)
    await cb.message.answer("📍 Отправьте геопозицию точки: скрепка 📎 → «Геопозиция» → передвиньте метку на вход "
                            "в магазин / зону разгрузки → «Отправить выбранную геопозицию».\n"
                            "Координаты сохранятся для этого адреса во всех будущих рейсах.", reply_markup=CANCEL_KB)


@router.message(Trip.geo, F.location, ADM)
async def admin_geo_save(message: Message, state: FSMContext):
    pid = (await state.get_data()).get("geo_pid")
    await state.clear()
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    N.set_geo(p, message.location.latitude, message.location.longitude, message.from_user.id)
    await message.answer(f"✅ Точка на карте сохранена для адреса {h(p['address'])}.", reply_markup=ADMIN_KB)
    text, markup = admin_point_card(pid)
    await message.answer(text, reply_markup=markup)


@router.message(Trip.geo, NOT_MENU, ADM)
async def admin_geo_wrong(message: Message):
    await message.answer("Нужна геопозиция: скрепка 📎 → «Геопозиция».")


# ------------------------------------------------------------ удаление рейса

@router.callback_query(F.data.startswith("td:"), ADM)
async def cb_trip_delete_ask(cb: CallbackQuery):
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (int(cb.data[3:]),))
    await cb.answer()
    if not wb or not wb["closed_at"]:
        await cb.message.answer("Удалить можно только закрытый рейс.")
        return
    c, t = S.wb_totals(wb["id"])
    nph = db.q1("SELECT COUNT(*) n FROM photos WHERE route_id IN (SELECT id FROM routes WHERE workbook_id=?)",
                (wb["id"],))["n"]
    await cb.message.answer(
        f"🗑 Удалить рейс <b>{h(S.wb_title(wb))}</b>?\n"
        f"Будут удалены: маршруты, {t} точек (закрыто {c}), отметки, ссылки на фото ({nph} доп.), проблемы, "
        f"сохранённый ZIP в списке «🗜 ZIP-архивы».\n"
        f"Постоянные примечания и заметки к адресам, водители и инструкции останутся.\n\n"
        f"Перед удалением бот пришлёт вам резервную копию — из неё рейс можно вернуть.",
        reply_markup=ikb([[("🗑 Да, удалить рейс", f"tdc:{wb['id']}")], [("Отмена", "noop")]]))


@router.callback_query(F.data.startswith("tdc:"), ADM)
async def cb_trip_delete(cb: CallbackQuery, bot: Bot):
    wb_id = int(cb.data[4:])
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (wb_id,))
    if not wb or not wb["closed_at"]:
        await cb.answer("Рейс не найден или не закрыт", show_alert=True)
        return
    await cb.answer("Удаляю…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    errors = await backup.send_backup(bot, f"перед удалением рейса «{S.wb_title(wb)}»", {cb.from_user.id})
    if errors:
        await cb.message.answer("❌ Не удалось сделать резервную копию — рейс не удалён.")
        return
    rids = [r["id"] for r in db.q("SELECT id FROM routes WHERE workbook_id=?", (wb_id,))]
    if rids:
        marks = ",".join("?" * len(rids))
        db.x(f"DELETE FROM photos WHERE route_id IN ({marks})", rids)
        db.x(f"DELETE FROM problems WHERE route_id IN ({marks})", rids)
        db.x(f"DELETE FROM points WHERE route_id IN ({marks})", rids)
    db.x("DELETE FROM routes WHERE workbook_id=?", (wb_id,))
    db.x("DELETE FROM zip_archives WHERE workbook_id=?", (wb_id,))
    db.x("DELETE FROM workbooks WHERE id=?", (wb_id,))
    try:
        from pathlib import Path
        f = Path(wb["path"])
        if f.exists() and not db.q1("SELECT 1 FROM workbooks WHERE path=?", (wb["path"],)):
            f.unlink()
    except Exception:
        pass
    await cb.message.answer(f"🗑 Рейс <b>{h(S.wb_title(wb))}</b> удалён. Резервная копия — выше.", reply_markup=ADMIN_KB)


# ------------------------------------------------------------ запросы водителя на закрытие без фото

@router.callback_query(F.data.startswith("pqa:"), ADM)
async def cb_point_req_approve(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data[4:])
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    if not p or p["status"] == "closed" or not p["close_req_at"]:
        await cb.answer("Запрос уже обработан", show_alert=True)
        return
    await cb.answer("Подтверждено")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    driver = p["close_req_by"]
    from handlers_driver import after_point_closed
    S.close_point(pid, driver or cb.from_user.id, "approved", p["close_req_comment"])
    r = S.route_with_wb(p["route_id"])
    c, t = S.route_counts(r["id"])
    await cb.message.answer(f"✅ Точка {p['seq']}. {h(p['address'])} закрыта без фото по запросу водителя. "
                            f"Маршрут №{h(r['route_no'])}: {c} из {t}.")
    if driver:
        try:
            await bot.send_message(driver, f"✅ Логист подтвердил закрытие точки {p['seq']}. {h(p['address'])} без фото.",
                                   reply_markup=ikb([[("🗺 К маршруту", f"r:{r['id']}")]]) if not r["closed_at"] else None)
        except Exception:
            pass
    await after_point_closed(bot, r["id"])


@router.callback_query(F.data.startswith("pqr:"), ADM)
async def cb_point_req_reject(cb: CallbackQuery, bot: Bot):
    pid = int(cb.data[4:])
    p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
    if not p or p["status"] == "closed" or not p["close_req_at"]:
        await cb.answer("Запрос уже обработан", show_alert=True)
        return
    db.x("UPDATE points SET close_req_at=NULL, close_req_by=NULL, close_req_comment=NULL WHERE id=?", (pid,))
    await cb.answer("Отклонено")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(f"❌ Закрытие точки {p['seq']}. {h(p['address'])} без фото отклонено, водитель уведомлён.")
    if p["close_req_by"]:
        try:
            await bot.send_message(p["close_req_by"],
                                   f"❌ Логист не подтвердил закрытие точки {p['seq']}. {h(p['address'])} без фото.\n"
                                   f"Загрузите фото или свяжитесь с логистом.",
                                   reply_markup=ikb([[("📍 Открыть точку", f"po:{pid}")]]))
        except Exception:
            pass


# ------------------------------------------------------------ ZIP-архивы --

@router.message(F.text.in_({A_ZIP, A_ZIP_OLD}))
async def zip_archive_menu(message: Message, state: FSMContext):
    await state.clear()
    admin = is_admin(message.from_user.id)
    archived = {w["id"]: w for w in S.archived_workbooks()}
    rows = [[(f"📦 {S.wb_title(w)} · {w['zip_at'][8:10]}.{w['zip_at'][5:7]}", f"zd:{w['id']}")]
            for w in archived.values()]
    if admin:
        rows += [[(f"🛠 Собрать ZIP: {S.date_label(w)}", f"zq2:{w['id']}")]
                 for w in S.workbooks(15) if w["id"] not in archived]
    if not rows:
        await message.answer("ZIP-архивов пока нет. Они появляются после закрытия рейса.")
        return
    await message.answer("🗜 <b>ZIP-архивы рейсов</b>\n📦 — сохранён в архиве, скачивается сразу"
                         + ("\n🛠 — ещё не собран" if admin else "") + ":", reply_markup=ikb(rows[:60]))


@router.callback_query(F.data.startswith("zd:"))
async def cb_zip_download(cb: CallbackQuery, bot: Bot):
    wb_id = int(cb.data[3:])
    parts = S.zip_archive(wb_id)
    await cb.answer()
    if not parts:
        await cb.message.answer("Архив не найден.")
        return
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (wb_id,))
    for z in parts:
        try:
            await bot.send_document(cb.message.chat.id, z["file_id"],
                                    caption=f"🗜 {h(S.wb_title(wb))}"
                                            + (f" — часть {z['part_no']} из {z['parts']}" if z["parts"] > 1 else "")
                                            + f"\nСобран {z['created_at'][:16]}")
        except Exception as e:  # noqa: BLE001
            await cb.message.answer(f"❌ Не удалось отправить часть {z['part_no']}: {h(str(e))}")
    if is_admin(cb.from_user.id):
        await cb.message.answer("Нужна свежая версия архива?", reply_markup=ikb([[("🔄 Пересобрать ZIP", f"zp:{wb_id}")]]))


@router.callback_query(F.data.startswith("zq2:"), ADM)
async def cb_zip_build(cb: CallbackQuery):
    wb_id = int(cb.data[4:])
    await cb.answer()
    if S.zip_archive(wb_id):
        await cb_zip_download(cb, cb.bot)
        return
    await zip_send(cb.message, wb_id)
