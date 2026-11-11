import sqlite3
from datetime import datetime

from config import DB_PATH, TZ

conn: sqlite3.Connection = None  # type: ignore[assignment]


def _open() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


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

def migrate() -> None:
    """Создаёт таблицы и добавляет новые колонки. Только добавляет — существующие данные не трогает."""
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
    # v6: важные примечания администратора и заметки водителей
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,             -- point / route
        point_key TEXT,                  -- 1c:<№ 1С> или addr:<адрес> (для scope=point)
        route_no TEXT,                   -- номер маршрута (для scope=route; для point — где оставлена)
        work_date TEXT,                  -- NULL = постоянно, иначе только на эту дату
        text TEXT NOT NULL,
        author_id INTEGER,
        author_role TEXT,                -- admin / driver
        status TEXT NOT NULL,            -- approved / pending / rejected / deleted
        created_at TEXT, decided_by INTEGER, decided_at TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_notes_point ON notes(point_key);
    CREATE INDEX IF NOT EXISTS ix_notes_route ON notes(route_no);
    """)
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
    # v9: названия рейсов, закрытие точки без фото (запрос/причина), ознакомление с инструкциями,
    # подтверждение сообщений, архив ZIP в канале
    _add_col("workbooks", "title", "title TEXT")
    _add_col("points", "close_req_at", "close_req_at TEXT")
    _add_col("points", "close_req_by", "close_req_by INTEGER")
    _add_col("points", "close_req_comment", "close_req_comment TEXT")
    _add_col("points", "close_note", "close_note TEXT")          # причина закрытия без фото
    _add_col("points", "closed_via", "closed_via TEXT")          # driver / admin / approved
    _add_col("instructions", "version_at", "version_at TEXT")
    _add_col("admin_messages", "kind", "kind TEXT")              # message / note
    _add_col("admin_messages", "admin_chat_id", "admin_chat_id INTEGER")
    _add_col("admin_messages", "summary_msg_id", "summary_msg_id INTEGER")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS instr_acks(
        instruction_id INTEGER, user_id INTEGER, acked_at TEXT,
        PRIMARY KEY(instruction_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS msg_deliveries(
        message_id INTEGER, user_id INTEGER, delivered_at TEXT, acked_at TEXT,
        PRIMARY KEY(message_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS zip_archives(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workbook_id INTEGER, part_no INTEGER, parts INTEGER, file_name TEXT,
        file_id TEXT, chat_id INTEGER, message_id INTEGER, created_at TEXT
    );
    """)
    conn.execute("UPDATE instructions SET version_at=COALESCE(updated_at, created_at) WHERE version_at IS NULL")
    # v14: фото акта приёма товара и возвратной накладной
    _add_col("points", "photo_act", "photo_act TEXT")
    _add_col("points", "photo_return", "photo_return TEXT")
    # v13: порядок объезда точек (как в маршрутном листе), по номеру маршрута и адресу
    conn.execute("""CREATE TABLE IF NOT EXISTS route_order(
        route_no TEXT, point_key TEXT, pos INTEGER, set_at TEXT, PRIMARY KEY(route_no, point_key))""")
    # v11: водители без Telegram (заводит администратор, отмечает точки за них); приглашение с переносом истории
    _add_col("users", "offline", "offline INTEGER DEFAULT 0")
    _add_col("invites", "merge_uid", "merge_uid INTEGER")
    # v10: точные координаты адреса для кнопки «Маршрут до точки» (общие для всех дат, как заметки)
    conn.execute("""CREATE TABLE IF NOT EXISTS point_geo(
        point_key TEXT PRIMARY KEY, lat REAL, lon REAL, set_by INTEGER, set_at TEXT)""")
    # перенос единственного фото из старых записей problems в photos
    conn.execute("""INSERT INTO photos(kind, point_id, route_id, problem_id, driver_id, file_id, file_type, created_at)
                    SELECT 'problem', point_id, route_id, id, driver_id, file_id, file_type, created_at
                    FROM problems WHERE file_id IS NOT NULL""")
    conn.execute("UPDATE problems SET file_id=NULL WHERE file_id IS NOT NULL")
    conn.commit()


def reload() -> None:
    """(Пере)подключение к базе — при старте и после восстановления из резервной копии."""
    global conn
    conn = _open()
    migrate()


reload()



def now() -> datetime:
    return datetime.now(TZ)


def now_s() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def q(sql: str, params=()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def q1(sql: str, params=()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


# True, если данные менялись после последней автокопии (см. backup.py)
dirty = False


def x(sql: str, params=()) -> int:
    global dirty
    cur = conn.execute(sql, params)
    conn.commit()
    dirty = True
    return cur.lastrowid


def xc(sql: str, params=()) -> int:
    """execute + commit, возвращает число изменённых строк"""
    global dirty
    cur = conn.execute(sql, params)
    conn.commit()
    if cur.rowcount:
        dirty = True
    return cur.rowcount


def get_setting(key: str, default: str = "") -> str:
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, value))
