"""Сборка ZIP-архива за дату: итоговый Excel + папки маршрутов с фото водителей."""
from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot

import db
import services as S
from config import EXPORT_DIR

# Telegram позволяет боту отправить файл до 50 МБ — берём с запасом
MAX_PART = 45 * 1024 * 1024
KINDS = (("photo_display", "выкладка"), ("photo_invoice", "накладная"), ("photo_poa", "доверенность"),
         ("photo_act", "акт_приёма"), ("photo_return", "возвратная_накладная"))

_lock = asyncio.Lock()


def safe(s: str | None, n: int = 40) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    return s[:n].strip() or "точка"


def ext(row) -> str:
    if row["file_type"] == "video":
        return Path(row["file_name"] or "").suffix or ".mp4"
    if row["file_type"] == "document":
        return Path(row["file_name"] or "").suffix or ".bin"
    return ".jpg"


def zip_base_name(wb) -> str:
    return datetime.strptime(wb["work_date"], "%Y-%m-%d").strftime("%d-%m-%Y")


def is_busy() -> bool:
    return _lock.locked()


async def build_zip(bot: Bot, wb_id: int,
                    progress: Callable[[int, int], Awaitable[None]] | None = None):
    """Возвращает (список путей к zip, временная папка, кол-во фото, список ошибок).
    Временную папку вызывающий удаляет после отправки."""
    async with _lock:
        wb = db.q1("SELECT * FROM workbooks WHERE id=?", (wb_id,))
        excel_path = S.export_workbook(wb_id)
        tmp = Path(tempfile.mkdtemp(prefix="zip_", dir=EXPORT_DIR))
        routes = S.routes_of(wb_id)

        jobs: list[tuple[str, str, str]] = []   # (папка маршрута, имя в архиве, file_id)
        texts: dict[str, str] = {}               # текстовые файлы в архиве (причины закрытия без фото)
        point_dirs: list[str] = []
        for r in routes:
            folder = safe(r["route_no"])
            # фото маршрутного листа — в папке маршрута
            for n, x in enumerate(S.sheet_photos(r["id"]), 1):
                jobs.append((folder, f"{folder}/маршрутный_лист_{n}{ext(x)}", x["file_id"]))
            for n, x in enumerate(db.q("SELECT * FROM photos WHERE kind='problem' AND route_id=? "
                                       "AND point_id IS NULL ORDER BY id", (r["id"],)), 1):
                jobs.append((folder, f"{folder}/проблема_без_адреса_{n}{ext(x)}", x["file_id"]))
            # папка на каждый адрес — фото, загруженные при закрытии точки
            for p in db.q("SELECT * FROM points WHERE route_id=? ORDER BY seq", (r["id"],)):
                pdir = f"{folder}/{p['seq']:02d}_{safe(p['address'], 50)}"
                point_dirs.append(pdir + "/")
                for i, (field, label) in enumerate(KINDS, 1):
                    if p[field]:
                        jobs.append((folder, f"{pdir}/{i}_{label}.jpg", p[field]))
                for n, x in enumerate(S.extra_photos(p["id"]), 1):
                    jobs.append((folder, f"{pdir}/6_доп_{n}{ext(x)}", x["file_id"]))
                for n, x in enumerate(db.q("SELECT * FROM photos WHERE kind='problem' AND point_id=? ORDER BY id",
                                           (p["id"],)), 1):
                    jobs.append((folder, f"{pdir}/проблема_{n}{ext(x)}", x["file_id"]))
                if p["close_note"]:
                    texts[f"{pdir}/причина_закрытия_без_фото.txt"] = p["close_note"]

        failed: list[str] = []
        done = 0
        sem = asyncio.Semaphore(5)

        async def download(idx: int, arc: str, file_id: str):
            nonlocal done
            dest = tmp / f"{idx}.bin"
            err = None
            async with sem:
                for attempt in range(3):
                    try:
                        await bot.download(file_id, destination=dest)
                        break
                    except Exception as e:  # noqa: BLE001
                        err = e
                        await asyncio.sleep(1 + attempt)
                else:
                    failed.append(f"{arc}: {err}")
            done += 1
            if progress:
                await progress(done, len(jobs))

        await asyncio.gather(*(download(i, arc, fid) for i, (_, arc, fid) in enumerate(jobs)))

        # раскладываем по частям не больше MAX_PART
        base = zip_base_name(wb)
        parts: list[Path] = []
        zf = None
        size = 0

        def new_part():
            nonlocal zf, size
            if zf:
                zf.close()
            path = tmp / f"part{len(parts) + 1}.zip"
            parts.append(path)
            zf = zipfile.ZipFile(path, "w")
            size = 0

        new_part()
        zf.write(excel_path, wb["filename"], compress_type=zipfile.ZIP_DEFLATED)
        size += excel_path.stat().st_size
        for r in routes:  # папка для каждого маршрута и адреса, даже если фото нет
            zf.writestr(zipfile.ZipInfo(f"{safe(r['route_no'])}/"), "")
        for d in point_dirs:
            zf.writestr(zipfile.ZipInfo(d), "")
        for name, txt in texts.items():
            zf.writestr(name, txt, compress_type=zipfile.ZIP_DEFLATED)
        if failed:
            zf.writestr("не_удалось_скачать.txt", "\n".join(failed), compress_type=zipfile.ZIP_DEFLATED)
        for idx, (_, arc, _fid) in enumerate(jobs):
            f = tmp / f"{idx}.bin"
            if not f.exists():
                continue
            fsize = f.stat().st_size
            if size and size + fsize > MAX_PART:
                new_part()
            zf.write(f, arc, compress_type=zipfile.ZIP_STORED)  # фото/видео уже сжаты
            size += fsize
            f.unlink()
        zf.close()

        final: list[Path] = []
        for n, p in enumerate(parts, 1):
            name = f"{base}.zip" if len(parts) == 1 else f"{base}_часть-{n}-из-{len(parts)}.zip"
            target = tmp / name
            p.rename(target)
            final.append(target)
        return final, tmp, len(jobs) - len(failed), failed


def cleanup(tmp: Path) -> None:
    shutil.rmtree(tmp, ignore_errors=True)
