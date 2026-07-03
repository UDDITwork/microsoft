"""Chat session CRUD, scoped to the authenticated user."""
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

import auth
import database
from models import CreateSessionRequest, SessionResponse

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _row_to_session(row) -> SessionResponse:
    return SessionResponse(
        id=row["id"],
        title=row["title"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        status=row["status"],
        extraction_status=row["extraction_status"],
    )


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    user: dict = Depends(auth.get_current_user),
    conn: aiosqlite.Connection = Depends(database.get_conn),
):
    cur = await conn.execute(
        """SELECT id, title, created_at, updated_at, status, extraction_status
           FROM chat_sessions WHERE user_id = ? AND status = 'active'
           ORDER BY updated_at DESC""",
        (user["user_id"],),
    )
    rows = await cur.fetchall()
    return [_row_to_session(r) for r in rows]


@router.post("", response_model=SessionResponse)
async def create_session(
    req: CreateSessionRequest,
    user: dict = Depends(auth.get_current_user),
    conn: aiosqlite.Connection = Depends(database.get_conn),
):
    session_id = str(uuid.uuid4())
    title = req.title or "New Session"
    await conn.execute(
        "INSERT INTO chat_sessions (id, user_id, title) VALUES (?, ?, ?)",
        (session_id, user["user_id"], title),
    )
    await conn.commit()
    cur = await conn.execute(
        """SELECT id, title, created_at, updated_at, status, extraction_status
           FROM chat_sessions WHERE id = ?""",
        (session_id,),
    )
    return _row_to_session(await cur.fetchone())


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    user: dict = Depends(auth.get_current_user),
    conn: aiosqlite.Connection = Depends(database.get_conn),
):
    cur = await conn.execute(
        """SELECT id, title, created_at, updated_at, status, extraction_status
           FROM chat_sessions WHERE id = ? AND user_id = ?""",
        (session_id, user["user_id"]),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return _row_to_session(row)


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    user: dict = Depends(auth.get_current_user),
    conn: aiosqlite.Connection = Depends(database.get_conn),
):
    if not await database.session_owned_by(conn, session_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="Session not found")
    # Archive (soft delete) — keeps data for audit while hiding from the list.
    await conn.execute(
        "UPDATE chat_sessions SET status = 'archived' WHERE id = ? AND user_id = ?",
        (session_id, user["user_id"]),
    )
    await conn.commit()
    return {"status": "archived", "session_id": session_id}
