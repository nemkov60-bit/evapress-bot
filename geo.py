"""Координаты адресов для «Маршрут до точки».

Порядок: точка, указанная администратором → кэш автопоиска → геокодер
(Яндекс Геокодер, если задан YANDEX_GEOCODER_KEY, иначе OpenStreetMap Nominatim).
Результаты кэшируются в point_geo (set_by=0 — найдено автоматически) и служат во всех будущих рейсах.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

import aiohttp

import db
import notes as N

log = logging.getLogger("geo")
YANDEX_KEY = os.getenv("YANDEX_GEOCODER_KEY", "").strip()
# Санкт-Петербург и Ленинградская область — результаты вне этой рамки отбрасываем
BBOX = (58.3, 61.9, 27.5, 36.0)  # lat_min, lat_max, lon_min, lon_max
_sem = asyncio.Semaphore(1)
_last_call = [0.0]

ABBR = [
    (r"\bпр-кт\b|\bпр\.-кт\b|\bпросп\.?(?=\s|,|$)", "проспект"), (r"\bпр\b\.?(?=\s|,|$)", "проспект"),
    (r"\bул\b\.?", "улица"), (r"\bш\b\.?(?=\s|,|$)", "шоссе"), (r"\bпер\b\.?", "переулок"),
    (r"\bб-р\b", "бульвар"), (r"\bнаб\b\.?", "набережная"), (r"\bпл\b\.?(?=\s|,|$)", "площадь"),
    (r"\bалл\b\.?", "аллея"), (r"\bтуп\b\.?", "тупик"), (r"\bдор\b\.?", "дорога"), (r"\bмкр\b\.?", "микрорайон"),
]


def clean_address(addr: str) -> str:
    a = re.sub(r"\(.*?\)", " ", addr or "")
    a = re.sub(r"\b(лит\.?\s*\w+|пом\.?\s*[\w-]+|п\.\s*\d+-?\w*)\b", " ", a, flags=re.I)
    a = re.sub(r"\bд\.\s*", "", a)
    for pat, rep in ABBR:
        a = re.sub(pat, rep, a, flags=re.I)
    a = re.sub(r"\s+", " ", a)
    a = re.sub(r"\s*,\s*(?:,\s*)*", ", ", a).strip(" ,.")
    # улица без населённого пункта («Космонавтов проспект, 28») — это Санкт-Петербург
    first = a.split(",")[0].lower()
    if re.search(r"улица|проспект|шоссе|переулок|бульвар|набережная|площадь|аллея|дорога|тупик|линия", first):
        a += ", Санкт-Петербург"
    return a


def _in_bbox(lat: float, lon: float) -> bool:
    return BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]


async def _yandex(session: aiohttp.ClientSession, q: str):
    url = "https://geocode-maps.yandex.ru/1.x/"
    params = {"apikey": YANDEX_KEY, "geocode": q, "format": "json", "results": "1", "lang": "ru_RU",
              "bbox": f"{BBOX[2]},{BBOX[0]}~{BBOX[3]},{BBOX[1]}", "rspn": "1"}
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json(content_type=None)
    members = data["response"]["GeoObjectCollection"]["featureMember"]
    if not members:
        return None
    lon, lat = map(float, members[0]["GeoObject"]["Point"]["pos"].split())
    return lat, lon


async def _nominatim(session: aiohttp.ClientSession, q: str):
    # правила Nominatim: не чаще 1 запроса в секунду и понятный User-Agent
    loop = asyncio.get_running_loop()
    wait = 1.1 - (loop.time() - _last_call[0])
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call[0] = loop.time()
    params = {"q": q, "format": "json", "limit": "1", "countrycodes": "ru", "accept-language": "ru",
              "viewbox": f"{BBOX[2]},{BBOX[1]},{BBOX[3]},{BBOX[0]}", "bounded": "1"}
    async with session.get("https://nominatim.openstreetmap.org/search", params=params,
                           headers={"User-Agent": "EVAPress-logistics-bot/1.0"},
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json(content_type=None)
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


async def geocode(address: str):
    q = clean_address(address)
    if not q:
        return None
    async with _sem, aiohttp.ClientSession() as session:
        try:
            res = await (_yandex(session, q) if YANDEX_KEY else _nominatim(session, q))
        except Exception as e:  # noqa: BLE001
            log.warning("geocode failed for %r: %s", q, e)
            return None
    if res and _in_bbox(*res):
        return res
    return None


def cached(p):
    """(lat, lon, источник) или None. Источник: 'admin' / 'auto'. Неудачный поиск помнится 7 дней."""
    g = N.geo(p)
    if not g or g["lat"] is None:
        return None
    return g["lat"], g["lon"], ("auto" if g["set_by"] == 0 else "admin")


def _recent_miss(p) -> bool:
    g = N.geo(p)
    if not g or g["lat"] is not None:
        return False
    try:
        return datetime.strptime(g["set_at"], "%Y-%m-%d %H:%M:%S") > datetime.now() - timedelta(days=7)
    except Exception:
        return False


async def coords(p):
    c = cached(p)
    if c:
        return c
    if _recent_miss(p):
        return None
    res = await geocode(p["address"])
    key = N.point_key(p)
    if res:
        db.x("INSERT INTO point_geo(point_key, lat, lon, set_by, set_at) VALUES(?,?,?,0,?) "
             "ON CONFLICT(point_key) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, set_by=0, set_at=excluded.set_at "
             "WHERE point_geo.set_by=0 OR point_geo.lat IS NULL", (key, res[0], res[1], db.now_s()))
        return res[0], res[1], "auto"
    db.x("INSERT INTO point_geo(point_key, lat, lon, set_by, set_at) VALUES(?,NULL,NULL,0,?) "
         "ON CONFLICT(point_key) DO UPDATE SET set_at=excluded.set_at WHERE point_geo.lat IS NULL",
         (key, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return None


async def prefetch(wb_id: int) -> tuple[int, int]:
    """Фоновый подбор координат для всех адресов рейса (после загрузки Excel)."""
    seen, found, total = set(), 0, 0
    for p in db.q("SELECT p.* FROM points p JOIN routes r ON r.id=p.route_id WHERE r.workbook_id=?", (wb_id,)):
        key = N.point_key(p)
        if key in seen:
            continue
        seen.add(key)
        total += 1
        try:
            if await coords(p):
                found += 1
        except Exception as e:  # noqa: BLE001
            log.warning("prefetch %s: %s", key, e)
    log.info("geo prefetch wb %s: %s of %s", wb_id, found, total)
    return found, total


NAV_HINT = ("👆 Нажмите на карту выше:\n"
            "• iPhone — «Маршрут» → выберите <b>Яндекс Навигатор</b>;\n"
            "• Android — значок маршрута / «Открыть в…» → <b>Навигатор</b>.\n"
            "Навигатор сразу построит маршрут от вашего местоположения.")


async def send_nav(bot, chat_id: int, p) -> None:
    """Присылает точку как геопозицию в Telegram — оттуда она открывается в Навигаторе с маршрутом."""
    from html import escape as h
    c = await coords(p)
    if c:
        await bot.send_venue(chat_id, c[0], c[1], title=f"{p['seq']}. {p['network'] or 'Точка'}"[:64],
                             address=(p["address"] or "")[:100])
        note = ("\n\n⚠️ Метка найдена автоматически по адресу — если она стоит не там, сообщите логисту."
                if c[2] == "auto" else "")
        await bot.send_message(chat_id, NAV_HINT + note)
    else:
        await bot.send_message(chat_id, "🧭 Не удалось найти координаты этого адреса.\n"
                                        "Нажмите на адрес ниже — он скопируется — и вставьте его в поиск Навигатора:\n\n"
                                        f"<code>{h(p['address'] or '')}</code>")
