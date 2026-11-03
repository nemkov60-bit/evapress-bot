import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent


def _load_env() -> None:
    p = BASE_DIR / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s;]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "bot.db"
WB_DIR = DATA_DIR / "workbooks"
EXPORT_DIR = DATA_DIR / "exports"
WRITE_PROBLEMS_TO_COMMENTS = os.getenv("WRITE_PROBLEMS_TO_COMMENTS", "1") == "1"
DRIVER_ROUTE_DAYS = int(os.getenv("DRIVER_ROUTE_DAYS", "2"))
# Время ежедневной резервной копии (ЧЧ:ММ, часовой пояс TZ_NAME). Пусто — не присылать.
BACKUP_TIME = os.getenv("BACKUP_TIME", "23:30").strip()

for d in (DATA_DIR, WB_DIR, EXPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)
