"""
Document upload + the extraction pipeline SSE trigger.

Upload validates (.docx only, max 2 per session), parses text, auto-detects the
doc role, and persists. Extraction is streamed separately via SSE so the UI can
show step-by-step progress.
"""
import json
import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

import auth
import config
import database
from models import DocumentResponse
from services import document_parser
from services.document_parser import DocumentParseError
from services.extractor import run_extraction_pipeline

router = APIRouter(prefix="/api/sessions", tags=["upload"])


async def _require_session(conn, session_id, user_id):
    if not await database.session_owned_by(conn, session_id, user_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("/{session_id}/upload")
async def upload_documents(
    session_id: str,
    files: list[UploadFile] = File(...),
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    await _require_session(conn, session_id, user["user_id"])

    # Enforce the 2-document ceiling across the session.
    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM uploaded_documents WHERE chat_session_id = ? AND user_id = ?",
        (session_id, user["user_id"]),
    )
    existing = (await cur.fetchone())["n"]
    if existing + len(files) > config.MAX_DOCS_PER_SESSION:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {config.MAX_DOCS_PER_SESSION} documents per session "
                   f"({existing} already uploaded).",
        )

    stored: list[DocumentResponse] = []
    for f in files:
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in config.ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Only .docx files are supported")

        data = await f.read()
        if len(data) > config.MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail=f"File too large: {f.filename}")

        try:
            raw_text, doc_type = await run_in_threadpool(document_parser.parse_and_detect, data)
        except DocumentParseError:
            raise HTTPException(
                status_code=400,
                detail=f"Could not read file: {f.filename}. Please ensure it is a valid .docx file.",
            )

        # Persist the original file to disk for traceability.
        safe_name = f"{session_id}_{uuid.uuid4().hex}{ext}"
        disk_path = os.path.join(config.UPLOAD_DIR, safe_name)
        await run_in_threadpool(_write_file, disk_path, data)

        cur = await conn.execute(
            """INSERT INTO uploaded_documents
               (chat_session_id, user_id, filename, file_path, doc_type, raw_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, user["user_id"], f.filename, disk_path, doc_type, raw_text),
        )
        await conn.commit()
        stored.append(DocumentResponse(
            id=cur.lastrowid, filename=f.filename, doc_type=doc_type, uploaded_at="now",
        ))

    return {"documents": [d.model_dump() for d in stored]}


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


@router.get("/{session_id}/documents", response_model=list[DocumentResponse])
async def list_documents(
    session_id: str,
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    await _require_session(conn, session_id, user["user_id"])
    cur = await conn.execute(
        """SELECT id, filename, doc_type, uploaded_at FROM uploaded_documents
           WHERE chat_session_id = ? AND user_id = ? ORDER BY id""",
        (session_id, user["user_id"]),
    )
    rows = await cur.fetchall()
    return [
        DocumentResponse(id=r["id"], filename=r["filename"], doc_type=r["doc_type"], uploaded_at=str(r["uploaded_at"]))
        for r in rows
    ]


@router.post("/{session_id}/extract")
async def run_extraction(
    session_id: str,
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    """Kick off the extraction pipeline and stream progress as SSE."""
    await _require_session(conn, session_id, user["user_id"])

    cur = await conn.execute(
        "SELECT COUNT(*) AS n FROM uploaded_documents WHERE chat_session_id = ? AND user_id = ?",
        (session_id, user["user_id"]),
    )
    if (await cur.fetchone())["n"] == 0:
        raise HTTPException(status_code=400, detail="No documents uploaded to extract from.")

    user_id = user["user_id"]

    async def event_stream():
        # Fresh connection so it lives for the full stream lifetime.
        stream_conn = await database.get_db()
        try:
            async for event in run_extraction_pipeline(stream_conn, session_id, user_id):
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


@router.get("/{session_id}/extraction-status")
async def extraction_status(
    session_id: str,
    user: dict = Depends(auth.get_current_user),
    conn: database.Connection = Depends(database.get_conn),
):
    await _require_session(conn, session_id, user["user_id"])
    cur = await conn.execute(
        "SELECT extraction_status FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user["user_id"]),
    )
    row = await cur.fetchone()

    # Include counts so a returning client can render the summary card.
    ccur = await conn.execute(
        """SELECT content_type, COUNT(*) AS n FROM extracted_content
           WHERE chat_session_id = ? AND user_id = ? GROUP BY content_type""",
        (session_id, user["user_id"]),
    )
    counts = {r["content_type"]: r["n"] for r in await ccur.fetchall()}
    return {"status": row["extraction_status"] if row else "none", "counts": counts}
