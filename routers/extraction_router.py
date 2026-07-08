"""Read endpoints for extracted content."""
from fastapi import APIRouter, Depends, HTTPException

import auth
import database
from models import ExtractedContentResponse

router = APIRouter(prefix="/api/sessions", tags=["extraction"])


async def _require_session(conn, session_id, user_id):
    if not await database.session_owned_by(conn, session_id, user_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/{session_id}/extracted", response_model=list[ExtractedContentResponse])
async def get_extracted(
    session_id: str,
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    await _require_session(conn, session_id, user["user_id"])
    cur = await conn.execute(
        """SELECT id, content_type, content_text, claim_number, metadata
           FROM extracted_content WHERE chat_session_id = ? AND user_id = ?
           ORDER BY id""",
        (session_id, user["user_id"]),
    )
    rows = await cur.fetchall()
    return [
        ExtractedContentResponse(
            id=r["id"], content_type=r["content_type"], content_text=r["content_text"],
            claim_number=r["claim_number"], metadata=r["metadata"],
        )
        for r in rows
    ]


@router.get("/{session_id}/extracted/claims", response_model=list[ExtractedContentResponse])
async def get_claims(
    session_id: str,
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    await _require_session(conn, session_id, user["user_id"])
    cur = await conn.execute(
        """SELECT id, content_type, content_text, claim_number, metadata
           FROM extracted_content
           WHERE chat_session_id = ? AND user_id = ?
             AND content_type IN (
                'claim_independent_1', 'claim_independent_system',
                'claim_independent_cpp', 'claim_dependent', 'all_claims_raw')
           ORDER BY claim_number IS NULL, claim_number, id""",
        (session_id, user["user_id"]),
    )
    rows = await cur.fetchall()
    return [
        ExtractedContentResponse(
            id=r["id"], content_type=r["content_type"], content_text=r["content_text"],
            claim_number=r["claim_number"], metadata=r["metadata"],
        )
        for r in rows
    ]
