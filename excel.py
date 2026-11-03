"""Работа с Excel-файлом маршрутов.

Чтение — через openpyxl. Запись (выгрузка) — точечной правкой XML внутри xlsx:
меняются ТОЛЬКО ячейки столбца «кол-во ТК» (и, по настройке, «комментарии»)
у строк с точками доставки. Все листы, стили, ширины столбцов, формулы,
лист «итого» и прочее остаются байт-в-байт как в исходном файле.
"""
from __future__ import annotations

import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import openpyxl
from openpyxl.utils import get_column_letter, column_index_from_string

HEADERS = {
    "point_no": ["№ точки", "№точки", "номер точки"],
    "network": ["сеть"],
    "code_1c": ["№ 1с", "№1с", "1с"],
    "address": ["адрес"],
    "qty": ["кол-во", "количество"],
    "price": ["цена"],
    "tk": ["кол-во тк", "кол-вотк"],
    "comment": ["комментарии", "комментарий"],
}


def norm(v) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip().lower()


def to_s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"[ \t\xa0]+", " ", str(v)).strip()


@dataclass
class ParsedPoint:
    row: int
    point_no: str
    network: str
    code_1c: str
    address: str
    qty: str
    price: str
    comment: str


@dataclass
class ParsedRoute:
    sheet: str
    route_no: str
    tk_col: str
    comment_col: str | None
    points: list[ParsedPoint] = field(default_factory=list)


def _find_header(ws):
    for r in range(1, min(ws.max_row, 15) + 1):
        found = {}
        for c in range(1, min(ws.max_column, 40) + 1):
            n = norm(ws.cell(r, c).value)
            for key, variants in HEADERS.items():
                if key not in found and n in variants:
                    found[key] = c
        if "address" in found and "tk" in found:
            return r, found
    return None, None


def _is_formula(v) -> bool:
    return isinstance(v, str) and v.startswith("=")


def parse_workbook(path: str | Path) -> list[ParsedRoute]:
    wb = openpyxl.load_workbook(path, data_only=False)
    routes: list[ParsedRoute] = []
    for ws in wb.worksheets:
        hr, cols = _find_header(ws)
        if not hr:
            continue  # лист «итого» и прочие служебные листы
        route = ParsedRoute(
            sheet=ws.title,
            route_no=ws.title.strip(),
            tk_col=get_column_letter(cols["tk"]),
            comment_col=get_column_letter(cols["comment"]) if "comment" in cols else None,
        )

        def val(r, key):
            return ws.cell(r, cols[key]).value if key in cols else None

        for r in range(hr + 1, ws.max_row + 1):
            row_vals = [ws.cell(r, c).value for c in cols.values()]
            if norm(val(r, "point_no")) == "итого" or any(_is_formula(v) for v in row_vals):
                break  # строка «итого» с формулами — конец списка точек
            address = to_s(val(r, "address"))
            if not address:
                continue
            route.points.append(ParsedPoint(
                row=r,
                point_no=to_s(val(r, "point_no")),
                network=to_s(val(r, "network")),
                code_1c=to_s(val(r, "code_1c")),
                address=address,
                qty=to_s(val(r, "qty")),
                price=to_s(val(r, "price")),
                comment=to_s(val(r, "comment")),
            ))
        if route.points:
            routes.append(route)
    return routes


# ---------------------------------------------------------------- export ---

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _sheet_parts(z: zipfile.ZipFile) -> dict[str, str]:
    """имя листа -> путь к xml внутри архива"""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {}
    for rel in rels.findall("rel:Relationship", _NS):
        t = rel.get("Target")
        t = t.lstrip("/") if t.startswith("/") else "xl/" + t
        targets[rel.get("Id")] = t
    res = {}
    for sh in wb.find("m:sheets", _NS).findall("m:sheet", _NS):
        rid = sh.get("{%s}id" % _NS["r"])
        res[sh.get("name")] = targets.get(rid)
    return res


def _split_ref(ref: str):
    m = re.match(r"([A-Z]+)(\d+)$", ref)
    return m.group(1), int(m.group(2))


def _new_cell(ref: str, style: str | None, value) -> str:
    s = f' s="{style}"' if style else ""
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{s}><v>{value}</v></c>'
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


_CELL_RE = r'<c\b[^>]*?\br="{ref}"[^>]*?(?:/>|>.*?</c>)'


def _set_cell(xml: str, ref: str, value) -> str:
    col, row = _split_ref(ref)
    row_re = re.compile(r'<row\b[^>]*?\br="%d"[^>]*?(?:/>|>.*?</row>)' % row, re.S)
    m = row_re.search(xml)
    if not m:
        # строки нет — вставляем новую по порядку
        new_row = f'<row r="{row}">{_new_cell(ref, None, value)}</row>'
        for rm in re.finditer(r'<row\b[^>]*?\br="(\d+)"', xml):
            if int(rm.group(1)) > row:
                return xml[:rm.start()] + new_row + xml[rm.start():]
        return xml.replace("</sheetData>", new_row + "</sheetData>")

    row_xml = m.group(0)
    if row_xml.endswith("/>") and "<c " not in row_xml:
        row_xml = row_xml[:-2] + "></row>"

    cm = re.search(_CELL_RE.format(ref=ref), row_xml, re.S)
    if cm:
        sm = re.search(r'\bs="(\d+)"', cm.group(0).split(">", 1)[0])
        new = _new_cell(ref, sm.group(1) if sm else None, value)
        row_xml = row_xml[:cm.start()] + new + row_xml[cm.end():]
    else:
        target = column_index_from_string(col)
        insert_at = None
        prev_style = None
        for c in re.finditer(r'<c\b[^>]*?\br="([A-Z]+)\d+"[^>]*?(?:/>|>.*?</c>)', row_xml, re.S):
            if column_index_from_string(c.group(1)) > target:
                insert_at = c.start()
                break
            sm = re.search(r'\bs="(\d+)"', c.group(0).split(">", 1)[0])
            prev_style = sm.group(1) if sm else None
        new = _new_cell(ref, prev_style, value)
        if insert_at is None:
            insert_at = row_xml.rfind("</row>")
        row_xml = row_xml[:insert_at] + new + row_xml[insert_at:]
    return xml[:m.start()] + row_xml + xml[m.end():]


def _refresh_sum_cache(xml: str, col: str, values: dict[int, int]) -> str:
    """Обновляет кэшированное значение формул SUM(Hx:Hy) в столбце ТК,
    чтобы итог был виден сразу (в т.ч. в предпросмотре Telegram)."""
    pat = re.compile(r'(<c r="%s(\d+)"[^>]*>)<f>(SUM\(%s(\d+):%s(\d+)\))</f>(?:<v>[^<]*</v>)?(</c>)' % (col, col, col))

    def repl(m):
        a, b = int(m.group(4)), int(m.group(5))
        total = sum(v for r, v in values.items() if a <= r <= b)
        return f"{m.group(1)}<f>{m.group(3)}</f><v>{total}</v>{m.group(6)}"

    return pat.sub(repl, xml)


def export_workbook(src: str | Path, dst: str | Path,
                    updates: dict[str, dict]) -> None:
    """updates: {имя_листа: {"tk_col": "H", "comment_col": "I",
                             "rows": {номер_строки: (0|1, текст_комментария|None)}}}"""
    src, dst = Path(src), Path(dst)
    tmp = dst.with_suffix(".tmp")
    with zipfile.ZipFile(src) as zin:
        parts = _sheet_parts(zin)
        changed: dict[str, bytes] = {}
        for sheet, upd in updates.items():
            part = parts.get(sheet)
            if not part:
                continue
            xml = zin.read(part).decode("utf-8")
            tk_vals = {}
            for row, (tk, comment) in sorted(upd["rows"].items()):
                xml = _set_cell(xml, f'{upd["tk_col"]}{row}', int(tk))
                tk_vals[row] = int(tk)
                if comment is not None and upd.get("comment_col"):
                    xml = _set_cell(xml, f'{upd["comment_col"]}{row}', comment)
            xml = _refresh_sum_cache(xml, upd["tk_col"], tk_vals)
            changed[part] = xml.encode("utf-8")
        # Excel пересчитает все формулы при открытии
        wbxml = zin.read("xl/workbook.xml").decode("utf-8")
        if "fullCalcOnLoad" not in wbxml:
            if "<calcPr" in wbxml:
                wbxml = wbxml.replace("<calcPr", '<calcPr fullCalcOnLoad="1"', 1)
            changed["xl/workbook.xml"] = wbxml.encode("utf-8")

        with zipfile.ZipFile(tmp, "w") as zout:
            for item in zin.infolist():
                data = changed.get(item.filename)
                if data is None:
                    data = zin.read(item.filename)
                zout.writestr(item, data)
    shutil.move(tmp, dst)
