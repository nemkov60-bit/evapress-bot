"""Важные примечания администратора и заметки водителей к адресам и маршрутам."""
from __future__ import annotations

import re
from html import escape as h

import db
import services as S


def point_key(p) -> str:
    """Ключ адреса, общий для всех дат: № 1С, а если его нет — адрес."""
    code = (p["code_1c"] or "").strip()
    if code:
        return f"1c:{code}"
    return "addr:" + re.sub(r"[\s.,]+", " ", (p["address"] or "").lower()).strip()


def _date_ok(sql_alias: str = "") -> str:
    return f"({sql_alias}work_date IS NULL OR {sql_alias}work_date=?)"


def for_point(p, work_date: str) -> list:
    return db.q(f"SELECT * FROM notes WHERE scope='point' AND point_key=? AND status='approved' AND {_date_ok()} "
                "ORDER BY author_role, id", (point_key(p), work_date))


def for_route(route_no: str, work_date: str) -> list:
    return db.q(f"SELECT * FROM notes WHERE scope='route' AND route_no=? AND status='approved' AND {_date_ok()} "
                "ORDER BY id", (route_no, work_date))


def points_with_notes(route_id: int, work_date: str) -> set[int]:
    res = set()
    for p in db.q("SELECT * FROM points WHERE route_id=?", (route_id,)):
        if for_point(p, work_date):
            res.add(p["id"])
    return res


def add(scope: str, text: str, author_id: int, role: str, status: str, point_key_: str | None = None,
        route_no: str | None = None, work_date: str | None = None) -> int:
    return db.x("INSERT INTO notes(scope, point_key, route_no, work_date, text, author_id, author_role, status, "
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (scope, point_key_, route_no, work_date, text, author_id, role, status, db.now_s()))


def decide(note_id: int, status: str, admin_id: int, only_if: str | None = None) -> bool:
    sql = "UPDATE notes SET status=?, decided_by=?, decided_at=? WHERE id=?"
    args = [status, admin_id, db.now_s(), note_id]
    if only_if:
        sql += " AND status=?"
        args.append(only_if)
    return db.xc(sql, args) == 1


def pending() -> list:
    return db.q("SELECT * FROM notes WHERE status='pending' ORDER BY id")


def get(note_id: int):
    return db.q1("SELECT * FROM notes WHERE id=?", (note_id,))


def when(n) -> str:
    return "постоянно" if not n["work_date"] else f"только {S.fmt_date(n['work_date'])}"


def render_block(admin_notes: list, driver_notes: list) -> str:
    """Блок для карточки точки."""
    out = []
    if admin_notes:
        out.append("❗ <b>ВАЖНО:</b>")
        out += [f"• {n['text']}" for n in admin_notes]
    if driver_notes:
        if out:
            out.append("")
        out.append("📝 <b>Заметки водителей:</b>")
        out += [f"• {n['text']} <i>— {h(S.driver_name(n['author_id']))}, {n['created_at'][8:10]}.{n['created_at'][5:7]}</i>"
                for n in driver_notes]
    return "\n".join(out)


def point_block(p, work_date: str) -> str:
    ns = for_point(p, work_date)
    return render_block([n for n in ns if n["author_role"] == "admin"], [n for n in ns if n["author_role"] != "admin"])


def route_block(route_no: str, work_date: str) -> str:
    ns = for_route(route_no, work_date)
    if not ns:
        return ""
    return "❗ <b>ВАЖНО ПО МАРШРУТУ:</b>\n" + "\n".join(f"• {n['text']}" for n in ns)
