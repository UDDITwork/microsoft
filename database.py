"""
Turso (libSQL) persistence layer.

Owns: connection lifecycle, schema creation, indexes, and seeding of the
instruction_prompts table with hot-swappable placeholder prompts.

There is no local SQLite file — every environment (local dev, CI, Cloud Run)
talks to the same remote Turso database via TURSO_DATABASE_URL/TURSO_AUTH_TOKEN.
The `libsql` client library is synchronous, so every call is offloaded to a
thread via `asyncio.to_thread` to keep the async request lifecycle non-blocking.

Every user-facing table carries both chat_session_id and user_id so that all
reads can be scoped to (session, user) for hard isolation.
"""
import asyncio

import libsql

import config

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id              TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    title           TEXT DEFAULT 'New Session',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status          TEXT DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    extraction_status TEXT DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS uploaded_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    filename        TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    doc_type        TEXT CHECK(doc_type IN ('idf', 'claims', 'unknown')),
    raw_text        TEXT NOT NULL,
    uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS extracted_content (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    content_type    TEXT NOT NULL CHECK(content_type IN (
                        'claim_independent_1',
                        'claim_independent_system',
                        'claim_independent_cpp',
                        'claim_dependent',
                        'all_claims_raw',
                        'background',
                        'technical_problems',
                        'invention_title',
                        'inventor_names',
                        'figure_descriptions'
                    )),
    content_text    TEXT NOT NULL,
    claim_number    INTEGER,
    metadata        TEXT,
    extracted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    role            TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    metadata        TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS generated_sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    section_type    TEXT NOT NULL CHECK(section_type IN (
                        'background',
                        'summary',
                        'technical_problems',
                        'technical_advantages',
                        'summary_paraphrasing',
                        'brief_description_drawings'
                    )),
    version         INTEGER DEFAULT 1,
    content         TEXT NOT NULL,
    generated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS instruction_prompts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    section_type    TEXT UNIQUE NOT NULL CHECK(section_type IN (
                        'background',
                        'summary',
                        'technical_problems',
                        'technical_advantages',
                        'summary_paraphrasing',
                        'brief_description_drawings'
                    )),
    system_prompt   TEXT NOT NULL,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Composite isolation indexes (session, user) on every dual-column table
CREATE INDEX IF NOT EXISTS idx_docs_session_user      ON uploaded_documents(chat_session_id, user_id);
CREATE INDEX IF NOT EXISTS idx_extracted_session_user ON extracted_content(chat_session_id, user_id);
CREATE INDEX IF NOT EXISTS idx_messages_session_user  ON chat_messages(chat_session_id, user_id);
CREATE INDEX IF NOT EXISTS idx_sections_session_user  ON generated_sections(chat_session_id, user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user          ON chat_sessions(user_id);

-- Content-type lookup index for the routing table
CREATE INDEX IF NOT EXISTS idx_extracted_session_type ON extracted_content(chat_session_id, content_type);
"""

# Placeholder prompts — hot-swappable via the admin API (PUT /api/prompts/{type}).
SEED_PROMPTS = {
    "background": "PLACEHOLDER — Replace with actual Background drafting instructions",
    "summary": "PLACEHOLDER — Replace with actual Summary drafting instructions",
    "technical_problems": "PLACEHOLDER — Replace with actual Technical Problems drafting instructions",
    "technical_advantages": "PLACEHOLDER — Replace with actual Technical Advantages drafting instructions",
    "summary_paraphrasing": "PLACEHOLDER — Replace with actual Summary Paraphrasing drafting instructions",
    "brief_description_drawings": "PLACEHOLDER — Replace with actual Brief Description of Drawings drafting instructions",
}


class Row:
    """Dict-and-index accessible row, mimicking aiosqlite.Row (libsql rows are plain tuples)."""

    __slots__ = ("_data", "_index")

    def __init__(self, data: tuple, columns: list[str]):
        self._data = data
        self._index = {name: i for i, name in enumerate(columns)}

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._data[self._index[key]]
        return self._data[key]

    def keys(self):
        return list(self._index.keys())

    def __repr__(self):
        return f"Row({dict(zip(self._index.keys(), self._data))})"


class Cursor:
    """Thin async wrapper around a libsql (sync) cursor result."""

    def __init__(self, raw_cursor):
        self._cursor = raw_cursor
        self._columns = [d[0] for d in (raw_cursor.description or [])]

    async def fetchone(self) -> "Row | None":
        row = await asyncio.to_thread(self._cursor.fetchone)
        return Row(row, self._columns) if row is not None else None

    async def fetchall(self) -> list["Row"]:
        rows = await asyncio.to_thread(self._cursor.fetchall)
        return [Row(r, self._columns) for r in rows]

    @property
    def lastrowid(self):
        return self._cursor.lastrowid


class Connection:
    """Thin async wrapper around a libsql (sync) connection to Turso."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    async def execute(self, sql: str, parameters: tuple = ()) -> Cursor:
        raw_cursor = await asyncio.to_thread(self._conn.execute, sql, parameters)
        return Cursor(raw_cursor)

    async def executescript(self, sql_script: str) -> None:
        await asyncio.to_thread(self._conn.executescript, sql_script)

    async def commit(self) -> None:
        await asyncio.to_thread(self._conn.commit)

    async def close(self) -> None:
        await asyncio.to_thread(self._conn.close)


async def get_db() -> Connection:
    """Open a new connection to the Turso database with foreign keys enabled."""
    if not config.TURSO_DATABASE_URL or not config.TURSO_AUTH_TOKEN:
        raise RuntimeError(
            "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set — all storage is "
            "managed by the Turso cloud database (no local SQLite fallback)."
        )
    raw = await asyncio.to_thread(
        libsql.connect, database=config.TURSO_DATABASE_URL, auth_token=config.TURSO_AUTH_TOKEN
    )
    conn = Connection(raw)
    await conn.execute("PRAGMA foreign_keys = ON")
    return conn


async def get_conn():
    """FastAPI dependency: yields a request-scoped connection and always closes it."""
    conn = await get_db()
    try:
        yield conn
    finally:
        await conn.close()


async def session_owned_by(conn: Connection, session_id: str, user_id: int) -> bool:
    """Isolation guard: True only if this session belongs to this user."""
    cur = await conn.execute(
        "SELECT 1 FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    return (await cur.fetchone()) is not None


async def init_db() -> None:
    """Create schema, indexes, and seed instruction prompts. Idempotent."""
    conn = await get_db()
    try:
        await conn.executescript(SCHEMA)
        for section_type, prompt in SEED_PROMPTS.items():
            # Insert only if missing — never clobber a user-edited prompt.
            await conn.execute(
                "INSERT OR IGNORE INTO instruction_prompts (section_type, system_prompt) VALUES (?, ?)",
                (section_type, prompt),
            )
        await conn.commit()
    finally:
        await conn.close()
