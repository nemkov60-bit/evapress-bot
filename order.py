"""Порядок объезда точек маршрута (как в маршрутном листе).

Сохраняется для номера маршрута по адресам (№ 1С / адрес), поэтому действует и в следующих рейсах.
Excel, отчёты и ZIP не меняются — там остаются номера точек из файла.
"""
from __future__ import annotations

from html import escape as h

import db
import notes as N
import services as S
from keyboards import ikb, short

# выбор в процессе: (uid, route_id) -> [point_id, ...]
_picks: dict[tuple[int, int], list[int]] = {}


def saved(route_no: str) -> dict[str, int]:
    return {r["point_key"]: r["pos"] for r in
            db.q("SELECT point_key, pos FROM route_order WHERE route_no=?", (route_no,))}


def ordered_points(rid: int) -> tuple[list, bool]:
    """Точки маршрута в порядке объезда. -> (точки, задан ли свой порядок)"""
    r = S.route_with_wb(rid)
    pts = db.q("SELECT * FROM points WHERE route_id=? ORDER BY seq", (rid,))
    pos = saved(r["route_no"])
    if not pos:
        return list(pts), False
    big = 10 ** 6
    return sorted(pts, key=lambda p: (pos.get(N.point_key(p), big), p["seq"])), True


def save(rid: int, pids: list[int]) -> None:
    r = S.route_with_wb(rid)
    db.x("DELETE FROM route_order WHERE route_no=?", (r["route_no"],))
    for i, pid in enumerate(pids, 1):
        p = db.q1("SELECT * FROM points WHERE id=?", (pid,))
        db.x("INSERT OR REPLACE INTO route_order(route_no, point_key, pos, set_at) VALUES(?,?,?,?)",
             (r["route_no"], N.point_key(p), i, db.now_s()))


def reset(rid: int) -> None:
    r = S.route_with_wb(rid)
    db.x("DELETE FROM route_order WHERE route_no=?", (r["route_no"],))


def start(uid: int, rid: int) -> None:
    _picks[(uid, rid)] = []


def pick(uid: int, rid: int, pid: int) -> None:
    lst = _picks.setdefault((uid, rid), [])
    if pid not in lst:
        lst.append(pid)


def undo(uid: int, rid: int) -> None:
    lst = _picks.setdefault((uid, rid), [])
    if lst:
        lst.pop()


def picks(uid: int, rid: int) -> list[int]:
    return _picks.get((uid, rid), [])


def finish(uid: int, rid: int) -> int:
    lst = _picks.pop((uid, rid), [])
    if lst:
        save(rid, lst)
    return len(lst)


def view(uid: int, rid: int, back: str):
    """Экран выбора порядка: нажимайте точки по очереди, как в маршрутном листе."""
    r = S.route_with_wb(rid)
    chosen = picks(uid, rid)
    pts = db.q("SELECT * FROM points WHERE route_id=? ORDER BY seq", (rid,))
    by_id = {p["id"]: p for p in pts}
    lines = [f"🔀 <b>Порядок точек — маршрут №{h(r['route_no'])}</b>",
             "Нажимайте адреса <b>по очереди, как в маршрутном листе</b>. Можно выбрать не все точки — "
             "остальные встанут ниже в порядке из файла. Затем «✅ Готово».", ""]
    if chosen:
        lines.append("<b>Новый порядок:</b>")
        lines += [f"{i}. {h(short(by_id[pid]['address'], 50))}" for i, pid in enumerate(chosen, 1) if pid in by_id]
    else:
        lines.append("<i>Пока ничего не выбрано.</i>")
    rows = [[(f"{p['seq']}. {short(p['address'], 44)}", f"orp:{rid}:{p['id']}")]
            for p in pts if p["id"] not in chosen]
    ctrl = []
    if chosen:
        ctrl.append(("↩️ Убрать последнюю", f"oru:{rid}"))
    ctrl.append(("✅ Готово", f"ork:{rid}"))
    rows.append(ctrl)
    rows.append([("🔄 Вернуть порядок из файла", f"orr:{rid}"), ("⬅️ Отмена", back)])
    return "\n".join(lines), ikb(rows[-97:] if len(rows) > 97 else rows)
