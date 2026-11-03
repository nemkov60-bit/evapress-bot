"""Бизнес-логика: импорт/экспорт Excel, статистика, отчёты."""
from __future__ import annotations

import re
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape as h
from pathlib import Path

import db
import excel
from config import ADMIN_IDS, EXPORT_DIR, WRITE_PROBLEMS_TO_COMMENTS, DRIVER_ROUTE_DAYS

DEFAULT_PROBLEM_HELP = (
    "⚠️ <b>Нестандартная ситуация на адресе</b>\n\n"
    "1. Не вступайте в конфликт, сохраняйте спокойствие.\n"
    "2. Сфотографируйте ситуацию (магазин закрыт, отказ в приёмке, нет места на полке и т.п.).\n"
    "3. Позвоните логисту по телефону ниже.\n"
    "4. Опишите ситуацию в боте — сообщение сразу получит логист.\n"
    "5. Не уезжайте с точки без согласования, если не можете закрыть точку.\n\n"
    "📞 <b>Контакты</b>\n"
    "Логист: +7 (___) ___-__-__\n"
    "Старший смены: +7 (___) ___-__-__\n\n"
    "<i>Текст редактирует администратор кнопкой «⚙️ Текст «Проблема»».</i>"
)


# ------------------------------------------------------------- dates -------

def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", text)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def fmt_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


def fmt_time(ts: str | None) -> str:
    return ts[11:16] if ts else ""


# ------------------------------------------------------------- users -------

def driver_name(uid: int | None) -> str:
    if not uid:
        return "не назначен"
    u = db.q1("SELECT full_name FROM users WHERE tg_id=?", (uid,))
    return u["full_name"] if u else f"id {uid}"


def drivers() -> list:
    return [u for u in db.q("SELECT * FROM users WHERE active=1 AND COALESCE(role,'driver')='driver' "
                            "ORDER BY full_name") if u["tg_id"] not in ADMIN_IDS]


def users_by_role() -> dict[str, list]:
    """Пользователи из базы по ролям (администраторы из ADMIN_IDS — отдельно, в handlers)."""
    res = {"admin": [], "customer": [], "driver": []}
    for u in db.q("SELECT * FROM users WHERE active=1 ORDER BY full_name"):
        if u["tg_id"] in ADMIN_IDS:
            continue
        res.setdefault(u["role"] or "driver", []).append(u)
    return res


# ------------------------------------------------------------- invites -----

INVITE_DAYS = 7


def create_invite(role: str, admin_id: int) -> str:
    code = secrets.token_urlsafe(9)
    expires = (db.now() + timedelta(days=INVITE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    db.x("INSERT INTO invites(code, role, created_by, created_at, expires_at) VALUES(?,?,?,?,?)",
         (code, role, admin_id, db.now_s(), expires))
    return code


def check_invite(code: str | None):
    if not code:
        return None
    return db.q1("SELECT * FROM invites WHERE code=? AND used_by IS NULL AND revoked=0 AND expires_at>?",
                 (code, db.now_s()))


def use_invite(code: str, uid: int) -> bool:
    return db.xc("UPDATE invites SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL AND revoked=0 "
                 "AND expires_at>?", (uid, db.now_s(), code, db.now_s())) == 1


def active_invites() -> list:
    return db.q("SELECT * FROM invites WHERE used_by IS NULL AND revoked=0 AND expires_at>? ORDER BY created_at DESC",
                (db.now_s(),))


# ------------------------------------------------------------- close day ---

def close_workbook(wb_id: int, admin_id: int) -> bool:
    return db.xc("UPDATE workbooks SET closed_at=?, closed_by=? WHERE id=? AND closed_at IS NULL",
                 (db.now_s(), admin_id, wb_id)) == 1


def request_route_close(rid: int, uid: int) -> bool:
    """Водитель просит закрыть рейс. False — запрос уже есть или рейс закрыт."""
    return db.xc("UPDATE routes SET close_req_at=?, close_req_by=? WHERE id=? AND closed_at IS NULL "
                 "AND close_req_at IS NULL", (db.now_s(), uid, rid)) == 1


def approve_route_close(rid: int, admin_id: int) -> bool:
    return db.xc("UPDATE routes SET closed_at=?, closed_by=?, close_req_at=NULL WHERE id=? AND closed_at IS NULL "
                 "AND close_req_at IS NOT NULL", (db.now_s(), admin_id, rid)) == 1


def reject_route_close(rid: int) -> bool:
    return db.xc("UPDATE routes SET close_req_at=NULL, close_req_by=NULL WHERE id=? AND closed_at IS NULL "
                 "AND close_req_at IS NOT NULL", (rid,)) == 1


def reopen_route(rid: int) -> None:
    db.x("UPDATE routes SET closed_at=NULL, closed_by=NULL, close_req_at=NULL, close_req_by=NULL WHERE id=?",
         (rid,))


def reopen_workbook(wb_id: int) -> None:
    db.x("UPDATE workbooks SET closed_at=NULL, closed_by=NULL WHERE id=?", (wb_id,))


def wb_totals(wb_id: int) -> tuple[int, int]:
    r = db.q1("SELECT COUNT(*) t, SUM(p.status='closed') c FROM points p JOIN routes r ON r.id=p.route_id "
              "WHERE r.workbook_id=?", (wb_id,))
    return r["c"] or 0, r["t"] or 0


def date_label(wb) -> str:
    return fmt_date(wb["work_date"]) + (" 🔒" if wb["closed_at"] else "")


# ------------------------------------------------------------- import ------

def import_workbook(path: Path, filename: str, work_date: date, admin_id: int) -> dict:
    parsed = excel.parse_workbook(path)
    if not parsed:
        raise ValueError("В файле не найдено ни одного листа с шапкой «адрес» / «кол-во ТК».")
    iso = work_date.isoformat()
    old_wb = db.q1("SELECT * FROM workbooks WHERE work_date=?", (iso,))
    kept_closed = 0
    old_assign: dict[str, tuple] = {}
    old_points: dict[tuple, list] = defaultdict(list)
    old_route_sheet: dict[int, str] = {}
    if old_wb:
        for r in db.q("SELECT * FROM routes WHERE workbook_id=?", (old_wb["id"],)):
            old_route_sheet[r["id"]] = r["sheet_name"]
            old_assign[r["sheet_name"]] = (r["driver_id"], r["assigned_at"], r["started_at"], r["finished_at"])
            for p in db.q("SELECT * FROM points WHERE route_id=? ORDER BY seq", (r["id"],)):
                key = (r["sheet_name"], p["code_1c"], p["address"], p["point_no"])
                old_points[key].append(dict(p))
        db.x("UPDATE workbooks SET filename=?, path=?, uploaded_at=?, uploaded_by=? WHERE id=?",
             (filename, str(path), db.now_s(), admin_id, old_wb["id"]))
        wb_id = old_wb["id"]
        db.x("DELETE FROM routes WHERE workbook_id=?", (wb_id,))
    else:
        wb_id = db.x("INSERT INTO workbooks(work_date, filename, path, uploaded_at, uploaded_by) VALUES(?,?,?,?,?)",
                     (iso, filename, str(path), db.now_s(), admin_id))

    id_map: dict[int, tuple[int, int]] = {}
    new_route_by_sheet: dict[str, int] = {}
    total_points = 0
    for r in parsed:
        drv, ass_at, st_at, fin_at = old_assign.get(r.sheet, (None, None, None, None))
        rid = db.x("INSERT INTO routes(workbook_id, route_no, sheet_name, tk_col, comment_col, driver_id,"
                   " assigned_at, started_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (wb_id, r.route_no, r.sheet, r.tk_col, r.comment_col, drv, ass_at, st_at, fin_at))
        new_route_by_sheet[r.sheet] = rid
        for seq, p in enumerate(r.points, 1):
            key = (r.sheet, p.code_1c, p.address, p.point_no)
            old = old_points[key].pop(0) if old_points.get(key) else None
            vals = (rid, seq, p.row, p.point_no, p.network, p.code_1c, p.address, p.qty, p.price, p.comment)
            if old:
                pid = db.x("INSERT INTO points(route_id, seq, row_idx, point_no, network, code_1c, address, qty, price,"
                           " orig_comment, status, opened_at, closed_at, closed_by, photo_display, photo_invoice,"
                           " photo_poa, poa_absent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           vals + (old["status"], old["opened_at"], old["closed_at"], old["closed_by"],
                                   old["photo_display"], old["photo_invoice"], old["photo_poa"], old["poa_absent"]))
                id_map[old["id"]] = (pid, rid)
                kept_closed += old["status"] == "closed"
            else:
                db.x("INSERT INTO points(route_id, seq, row_idx, point_no, network, code_1c, address, qty, price,"
                     " orig_comment) VALUES(?,?,?,?,?,?,?,?,?,?)", vals)
            total_points += 1
    # перепривязываем проблемы к новым id точек
    for old_rid, sheet in old_route_sheet.items():
        for table in ("problems", "photos"):
            for pr in db.q(f"SELECT id, point_id FROM {table} WHERE route_id=?", (old_rid,)):
                new_pid, new_rid = id_map.get(pr["point_id"], (None, new_route_by_sheet.get(sheet)))
                db.x(f"UPDATE {table} SET point_id=?, route_id=? WHERE id=?", (new_pid, new_rid, pr["id"]))
    return {
        "date": iso, "replaced": bool(old_wb), "routes": [(r.route_no, len(r.points)) for r in parsed],
        "points": total_points, "kept_closed": kept_closed,
    }


# ------------------------------------------------------------- photos ------

def add_photo(kind: str, file_id: str, file_type: str, driver_id: int, point_id=None, route_id=None,
              problem_id=None, file_name: str | None = None) -> int:
    return db.x("INSERT INTO photos(kind, point_id, route_id, problem_id, driver_id, file_id, file_type, file_name,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (kind, point_id, route_id, problem_id, driver_id, file_id, file_type, file_name, db.now_s()))


def extra_photos(point_id: int) -> list:
    return db.q("SELECT * FROM photos WHERE kind='extra' AND point_id=? ORDER BY id", (point_id,))


def problem_photos(problem_id: int) -> list:
    return db.q("SELECT * FROM photos WHERE kind='problem' AND problem_id=? ORDER BY id", (problem_id,))


# ------------------------------------------------------------- export ------

def problems_by_point(route_ids: list[int]) -> dict[int, list]:
    if not route_ids:
        return {}
    marks = ",".join("?" * len(route_ids))
    res = defaultdict(list)
    for pr in db.q(f"SELECT * FROM problems WHERE route_id IN ({marks}) AND point_id IS NOT NULL ORDER BY id",
                   route_ids):
        res[pr["point_id"]].append(pr)
    return res


def export_workbook(wb_id: int) -> Path:
    wb = db.q1("SELECT * FROM workbooks WHERE id=?", (wb_id,))
    routes = db.q("SELECT * FROM routes WHERE workbook_id=?", (wb_id,))
    probs = problems_by_point([r["id"] for r in routes])
    updates = {}
    for r in routes:
        rows = {}
        for p in db.q("SELECT * FROM points WHERE route_id=?", (r["id"],)):
            tk = 1 if p["status"] == "closed" else 0
            comment = None
            if WRITE_PROBLEMS_TO_COMMENTS and probs.get(p["id"]):
                parts = [p["orig_comment"]] if p["orig_comment"] else []
                for pr in probs[p["id"]]:
                    n = len(problem_photos(pr["id"]))
                    parts.append(f'{fmt_time(pr["created_at"])} {pr["text"] or ""}'.strip()
                                 + (f" (фото: {n})" if n else ""))
                comment = "; ".join(parts)
            rows[p["row_idx"]] = (tk, comment)
        updates[r["sheet_name"]] = {"tk_col": r["tk_col"], "comment_col": r["comment_col"], "rows": rows}
    dst = EXPORT_DIR / wb["filename"]
    src = Path(wb["path"])
    if not src.exists():  # база перенесена на другой сервер / в другую папку
        from config import WB_DIR
        src = WB_DIR / src.name
    excel.export_workbook(src, dst, updates)
    return dst


# ------------------------------------------------------------- queries -----

def workbooks(limit: int = 10) -> list:
    return db.q("SELECT * FROM workbooks ORDER BY work_date DESC LIMIT ?", (limit,))


def workbook_by_date(d: date):
    return db.q1("SELECT * FROM workbooks WHERE work_date=?", (d.isoformat(),))


def latest_workbook():
    """Сегодняшний файл, а если его нет — самый свежий."""
    today = db.now().date().isoformat()
    return db.q1("SELECT * FROM workbooks WHERE work_date=?", (today,)) or \
        db.q1("SELECT * FROM workbooks ORDER BY work_date DESC LIMIT 1")


def routes_of(wb_id: int) -> list:
    rows = db.q("SELECT * FROM routes WHERE workbook_id=?", (wb_id,))
    return sorted(rows, key=lambda r: route_sort_key(r["route_no"]))


def route_sort_key(no: str):
    m = re.match(r"\d+", no)
    return (0, int(m.group()), no) if m else (1, 0, no)


def route_counts(route_id: int) -> tuple[int, int]:
    r = db.q1("SELECT COUNT(*) t, SUM(status='closed') c FROM points WHERE route_id=?", (route_id,))
    return r["c"] or 0, r["t"] or 0


def route_with_wb(route_id: int):
    return db.q1("SELECT r.*, w.work_date, w.closed_at AS wb_closed_at FROM routes r "
                 "JOIN workbooks w ON w.id=r.workbook_id WHERE r.id=?",
                 (route_id,))


def driver_routes(uid: int) -> list:
    """Маршруты водителя: сегодняшние + незавершённые за последние N дней."""
    since = (db.now().date() - timedelta(days=DRIVER_ROUTE_DAYS)).isoformat()
    rows = db.q("SELECT r.*, w.work_date FROM routes r JOIN workbooks w ON w.id=r.workbook_id "
                "WHERE r.driver_id=? AND w.work_date>=? AND w.closed_at IS NULL AND r.closed_at IS NULL "
                "ORDER BY w.work_date DESC", (uid, since))
    return list(rows)  # пока водитель или админ не закрыл рейс, маршрут остаётся у водителя


def route_drivers(route) -> list[str]:
    ids = []
    if route["driver_id"]:
        ids.append(route["driver_id"])
    for p in db.q("SELECT DISTINCT closed_by FROM points WHERE route_id=? AND closed_by IS NOT NULL", (route["id"],)):
        if p["closed_by"] not in ids:
            ids.append(p["closed_by"])
    return [driver_name(i) for i in ids]


def route_state(route) -> str:
    c, t = route_counts(route["id"])
    if route["closed_at"]:
        return f"🏁 рейс закрыт в {fmt_time(route['closed_at'])}"
    if route["close_req_at"]:
        return f"⏳ водитель просит закрыть рейс (с {fmt_time(route['close_req_at'])})"
    if t and c == t:
        return "✅ выполнен"
    if route["started_at"] or c:
        return "🚚 в пути"
    return "⏸ не начат"


# ------------------------------------------------------------- texts -------

def stats_text(wb, route_nos: list[str] | None = None) -> str:
    routes = routes_of(wb["id"])
    if route_nos:
        wanted = {n.strip().lower() for n in route_nos}
        routes = [r for r in routes if r["route_no"].lower() in wanted]
        if not routes:
            return f"Маршруты {', '.join(route_nos)} не найдены в файле на {fmt_date(wb['work_date'])}."
    lines = [f"📊 <b>Статистика на {fmt_date(wb['work_date'])}</b>"
             + (" · рейс закрыт 🔒" if wb["closed_at"] else ""), ""]
    tc = tt = 0
    for r in routes:
        c, t = route_counts(r["id"])
        tc += c
        tt += t
        pct = round(c * 100 / t) if t else 0
        lines.append(f"<b>№{h(r['route_no'])}</b> — {h(driver_name(r['driver_id']))}\n"
                     f"   закрыто {c}/{t} ({pct}%), не закрыто {t - c} · {route_state(r)}")
    pct = round(tc * 100 / tt) if tt else 0
    lines += ["", f"<b>Итого:</b> закрыто {tc} из {tt} ({pct}%), не закрыто {tt - tc}"]
    return "\n".join(lines)


def report_text(wb, route) -> str:
    c, t = route_counts(route["id"])
    names = route_drivers(route)
    lines = [
        f"📑 <b>Отчёт: {fmt_date(wb['work_date'])}, маршрут №{h(route['route_no'])}</b>",
        f"👤 Водитель(и): {h(', '.join(names)) if names else 'не назначен'}",
        f"Статус: {route_state(route)}",
    ]
    if route["started_at"]:
        lines.append(f"Начат: {route['started_at'][:16]}")
    if route["finished_at"]:
        lines.append(f"Завершён: {route['finished_at'][:16]}")
    lines += [f"Всего точек: <b>{t}</b>", f"✅ Закрыто: <b>{c}</b>", f"❌ Не закрыто: <b>{t - c}</b>"]
    open_pts = db.q("SELECT * FROM points WHERE route_id=? AND status!='closed' ORDER BY seq", (route["id"],))
    if open_pts:
        lines.append("\n<b>Незакрытые точки:</b>")
        for p in open_pts:
            lines.append(f"{p['seq']}. {h(p['network'])} — {h(p['address'])}")
    probs = db.q("SELECT pr.*, p.address FROM problems pr LEFT JOIN points p ON p.id=pr.point_id "
                 "WHERE pr.route_id=? ORDER BY pr.id", (route["id"],))
    if probs:
        lines.append(f"\n<b>Проблемы на адресах ({len(probs)}):</b>")
        for pr in probs:
            n = len(problem_photos(pr["id"]))
            lines.append(f"• {fmt_time(pr['created_at'])} {h(pr['address'] or 'без точки')} — "
                         f"{h(pr['text'] or '(без текста)')}" + (f" 📷{n}" if n else "")
                         + f" ({h(driver_name(pr['driver_id']))})")
    return "\n".join(lines)


def report_all_text(wb) -> str:
    routes = routes_of(wb["id"])
    lines = [f"📑 <b>Отчёт на {fmt_date(wb['work_date'])} по всем маршрутам</b>"
             + (" · рейс закрыт 🔒" if wb["closed_at"] else ""), ""]
    tc = tt = 0
    for r in routes:
        c, t = route_counts(r["id"])
        tc += c
        tt += t
        names = route_drivers(r)
        lines.append(f"<b>№{h(r['route_no'])}</b>: закрыто {c}, не закрыто {t - c} из {t}\n"
                     f"   👤 {h(', '.join(names)) if names else 'не назначен'}")
    lines += ["", f"<b>Итого:</b> закрыто {tc}, не закрыто {tt - tc} из {tt}"]
    return "\n".join(lines)


def point_text(p, route=None) -> str:
    route = route or route_with_wb(p["route_id"])
    status = "✅ Закрыта" + (f" в {fmt_time(p['closed_at'])}" if p["closed_at"] else "") \
        if p["status"] == "closed" else "⬜ Не закрыта"
    lines = [
        f"📍 <b>Точка {p['seq']} — маршрут №{h(route['route_no'])} ({fmt_date(route['work_date'])})</b>",
        "",
        f"🏠 <b>Адрес:</b> {h(p['address'])}",
        f"🏪 <b>Сеть:</b> {h(p['network'] or '—')}",
    ]
    if p["point_no"]:
        lines.append(f"🔢 <b>№ точки:</b> {h(p['point_no'])}")
    lines.append(f"🧾 <b>№ 1С:</b> {h(p['code_1c'] or '—')}")
    lines.append(f"📦 <b>Кол-во:</b> {h(p['qty'] or '—')}")
    if p["orig_comment"]:
        lines.append(f"💬 <b>Комментарий:</b> {h(p['orig_comment'])}")
    lines += ["", f"<b>Статус:</b> {status}", photos_progress(p)]
    return "\n".join(lines)


def photos_progress(p) -> str:
    def mark(ok):
        return "✅" if ok else "▫️"
    poa = "✅ Фото доверенности на возврат" if p["photo_poa"] else (
        "➖ Доверенность отсутствует" if p["poa_absent"] else "▫️ Фото доверенности на возврат")
    n_extra = len(extra_photos(p["id"]))
    return (f"{mark(p['photo_display'])} Фото выкладки\n"
            f"{mark(p['photo_invoice'])} Фото накладной\n"
            f"{poa}"
            + (f"\n📎 Дополнительные фото: {n_extra}" if n_extra else ""))


def next_step(p) -> str | None:
    if not p["photo_display"]:
        return "display"
    if not p["photo_invoice"]:
        return "invoice"
    if not p["photo_poa"] and not p["poa_absent"]:
        return "poa"
    return None
