"""Read endpoints for generated sections."""
from fastapi import APIRouter, Depends, HTTPException

import auth
import database
from models import SectionResponse

router = APIRouter(prefix="/api/sessions", tags=["sections"])


async def _require_session(conn, session_id, user_id):
    if not await database.session_owned_by(conn, session_id, user_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/{session_id}/sections", response_model=list[SectionResponse])
async def list_sections(
    session_id: str,
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    """Latest version of each generated section type."""
    await _require_session(conn, session_id, user["user_id"])
    cur = await conn.execute(
        """SELECT gs.id, gs.section_type, gs.version, gs.content, gs.generated_at
           FROM generated_sections gs
           JOIN (
               SELECT section_type, MAX(version) AS mv FROM generated_sections
               WHERE chat_session_id = ? AND user_id = ? GROUP BY section_type
           ) latest
           ON gs.section_type = latest.section_type AND gs.version = latest.mv
           WHERE gs.chat_session_id = ? AND gs.user_id = ?
           ORDER BY gs.generated_at""",
        (session_id, user["user_id"], session_id, user["user_id"]),
    )
    rows = await cur.fetchall()
    return [
        SectionResponse(
            id=r["id"], section_type=r["section_type"], version=r["version"],
            content=r["content"], generated_at=str(r["generated_at"]),
        )
        for r in rows
    ]


@router.get("/{session_id}/sections/{section_type}", response_model=SectionResponse)
async def get_section(
    session_id: str,
    section_type: str,
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    """Latest version of one section type."""
    await _require_session(conn, session_id, user["user_id"])
    cur = await conn.execute(
        """SELECT id, section_type, version, content, generated_at
           FROM generated_sections
           WHERE chat_session_id = ? AND user_id = ? AND section_type = ?
           ORDER BY version DESC LIMIT 1""",
        (session_id, user["user_id"], section_type),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Section not generated yet")
    return SectionResponse(
        id=row["id"], section_type=row["section_type"], version=row["version"],
        content=row["content"], generated_at=str(row["generated_at"]),
    )
