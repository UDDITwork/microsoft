"""
Admin endpoints for instruction prompts (hot-swappable drafting instructions).

Any authenticated user may read/update the shared instruction prompts. These are
global (not per-session): they define HOW each section is drafted.
"""
from fastapi import APIRouter, Depends, HTTPException

import auth
import database
from models import UpdatePromptRequest, PromptResponse
from services.section_router_logic import SECTION_TYPES

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.get("", response_model=list[PromptResponse])
async def list_prompts(
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    cur = await conn.execute(
        "SELECT section_type, system_prompt, updated_at FROM instruction_prompts ORDER BY section_type"
    )
    rows = await cur.fetchall()
    return [
        PromptResponse(section_type=r["section_type"], system_prompt=r["system_prompt"], updated_at=str(r["updated_at"]))
        for r in rows
    ]


@router.put("/{section_type}", response_model=PromptResponse)
async def update_prompt(
    section_type: str,
    req: UpdatePromptRequest,
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    if section_type not in SECTION_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown section_type '{section_type}'")

    await conn.execute(
        """INSERT INTO instruction_prompts (section_type, system_prompt, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(section_type)
           DO UPDATE SET system_prompt = excluded.system_prompt, updated_at = CURRENT_TIMESTAMP""",
        (section_type, req.system_prompt),
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT section_type, system_prompt, updated_at FROM instruction_prompts WHERE section_type = ?",
        (section_type,),
    )
    row = await cur.fetchone()
    return PromptResponse(section_type=row["section_type"], system_prompt=row["system_prompt"], updated_at=str(row["updated_at"]))
