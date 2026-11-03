import sqlite3
from datetime import datetime

from config import DB_PATH, TZ

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    username TEXT,
    phone TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS instructions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT,
    file_id TEXT,
    file_type TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS workbooks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_date TEXT UNIQUE NOT NULL,      -- YYYY-MM-DD
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    uploaded_at TEXT,
    uploaded_by INTEGER
);
CREATE TABLE IF NOT EXISTS routes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workbook_id INTEGER NOT NULL REFERENCES workbooks(id) ON DELETE CASCADE,
    route_no TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    tk_col TEXT NOT NULL,
    comment_col TEXT,
    driver_id INTEGER,
    assigned_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(workbook_id, sheet_name)
);
CREATE TABLE IF NOT EXISTS points(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    row_idx INTEGER NOT NULL,
    point_no TEXT, network TEXT, code_1c TEXT, address TEXT,
    qty TEXT, price TEXT, orig_comment TEXT,
    status TEXT DEFAULT 'open',          -- open / closed
    opened_at TEXT, closed_at TEXT, closed_by INTEGER,
    photo_display TEXT,                  -- фото выкладки
    photo_invoice TEXT,                  -- фото накладной
    photo_poa TEXT,                      -- фото доверенности на возврат
    poa_absent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS problems(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER, route_id INTEGER, driver_id INTEGER,
    text TEXT, file_id TEXT, file_type TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS admin_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER, target TEXT, text TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_points_route ON points(route_id);
CREATE INDEX IF NOT EXISTS ix_routes_driver ON routes(driver_id);
"""
conn.executescript(SCHEMA)


def _add_col(table: str, col: str, ddl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# миграции v2: роли, приглашения, закрытие рейса
_add_col("users", "role", "role TEXT DEFAULT 'driver'")        # admin / customer / driver
_add_col("workbooks", "closed_at", "closed_at TEXT")
_add_col("workbooks", "closed_by", "closed_by INTEGER")
# v4: водитель сам закрывает свой рейс (маршрут)
_add_col("routes", "closed_at", "closed_at TEXT")
_add_col("routes", "closed_by", "closed_by INTEGER")
# v5: запрос водителя на закрытие рейса, ждёт решения администратора
_add_col("routes", "close_req_at", "close_req_at TEXT")
_add_col("routes", "close_req_by", "close_req_by INTEGER")
conn.executescript("""
CREATE TABLE IF NOT EXISTS invites(
    code TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    created_by INTEGER,
    created_at TEXT,
    expires_at TEXT,
    used_by INTEGER,
    used_at TEXT,
    revoked INTEGER DEFAULT 0
);
-- v3: дополнительные фото к точке и фото к «Проблеме на адресе», без ограничения по количеству
CREATE TABLE IF NOT EXISTS photos(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- extra / problem
    point_id INTEGER, route_id INTEGER, problem_id INTEGER, driver_id INTEGER,
    file_id TEXT NOT NULL, file_type TEXT, file_name TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_photos_point ON photos(point_id);
CREATE INDEX IF NOT EXISTS ix_photos_problem ON photos(problem_id);
""")
# перенос единственного фото из старых записей problems в photos
conn.execute("""INSERT INTO photos(kind, point_id, route_id, problem_id, driver_id, file_id, file_type, created_at)
                SELECT 'problem', point_id, route_id, id, driver_id, file_id, file_type, created_at
                FROM problems WHERE file_id IS NOT NULL""")
conn.execute("UPDATE problems SET file_id=NULL WHERE file_id IS NOT NULL")
conn.commit()


def now() -> datetime:
    return datetime.now(TZ)


def now_s() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def q(sql: str, params=()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def q1(sql: str, params=()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def x(sql: str, params=()) -> int:
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid


def xc(sql: str, params=()) -> int:
    """execute + commit, возвращает число изменённых строк"""
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount


def get_setting(key: str, default: str = "") -> str:
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, value))
