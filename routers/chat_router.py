"""Chat endpoint (SSE streaming) + paginated message history."""
import json

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

import auth
import database
from models import ChatRequest, MessageResponse
from services import chat_service

router = APIRouter(prefix="/api/sessions", tags=["chat"])


@router.post("/{session_id}/chat")
async def chat(
    session_id: str,
    req: ChatRequest,
    user: dict = Depends(auth.get_current_user),
    conn: aiosqlite.Connection = Depends(database.get_conn),
):
    if not await database.session_owned_by(conn, session_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="Session not found")
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    user_id = user["user_id"]
    message = req.message
    section_type = req.section_type

    async def event_stream():
        stream_conn = await database.get_db()
        try:
            async for event in chat_service.stream_chat_turn(
                stream_conn, session_id, user_id, message, section_type
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            await stream_conn.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    session_id: str,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(auth.get_current_user),
    conn: aiosqlite.Connection = Depends(database.get_conn),
):
    if not await database.session_owned_by(conn, session_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="Session not found")
    cur = await conn.execute(
        """SELECT id, role, content, metadata, created_at FROM chat_messages
           WHERE chat_session_id = ? AND user_id = ? AND role IN ('user', 'assistant')
           ORDER BY id ASC LIMIT ? OFFSET ?""",
        (session_id, user["user_id"], limit, offset),
    )
    rows = await cur.fetchall()
    return [
        MessageResponse(
            id=r["id"], role=r["role"], content=r["content"],
            metadata=r["metadata"], created_at=str(r["created_at"]),
        )
        for r in rows
    ]
