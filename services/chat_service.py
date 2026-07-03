"""
Chat orchestration.

Assembles the per-turn context (via section_router_logic), loads the last N
messages, streams the Claude response, and persists both messages plus — when a
section was requested — a versioned row in generated_sections.
"""
import json
from typing import AsyncIterator, Optional

import aiosqlite

import config
from services import llm
from services import section_router_logic as router

# Keyword → section_type for free-text section requests (button clicks pass it explicitly).
_SECTION_KEYWORDS = [
    ("technical_advantages", ["technical advantage", "technical advantages", "advantages"]),
    ("technical_problems", ["technical problem", "technical problems", "problems"]),
    ("summary_paraphrasing", ["summary paraphrasing", "paraphrasing", "paraphrase"]),
    ("brief_description_drawings", ["brief description of drawings", "description of drawings", "drawings"]),
    ("background", ["background"]),
    ("summary", ["summary"]),
]


def detect_section_type(message: str) -> Optional[str]:
    lowered = message.lower()
    # "generate the X section" style — match the most specific keywords first.
    for section_type, keywords in _SECTION_KEYWORDS:
        for kw in keywords:
            if kw in lowered:
                return section_type
    return None


async def _load_history(conn, session_id, user_id) -> list[dict]:
    cur = await conn.execute(
        """SELECT role, content FROM chat_messages
           WHERE chat_session_id = ? AND user_id = ? AND role IN ('user', 'assistant')
           ORDER BY id DESC LIMIT ?""",
        (session_id, user_id, config.CHAT_HISTORY_LIMIT),
    )
    rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """
    Ensure the Anthropic messages array is valid: starts with a user turn and has
    no consecutive same-role messages (merged if they occur).
    """
    # Drop leading assistant messages.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    normalized: list[dict] = []
    for m in messages:
        if normalized and normalized[-1]["role"] == m["role"]:
            normalized[-1]["content"] += "\n\n" + m["content"]
        else:
            normalized.append({"role": m["role"], "content": m["content"]})
    return normalized


async def _store_message(conn, session_id, user_id, role, content, metadata=None) -> int:
    cur = await conn.execute(
        """INSERT INTO chat_messages (chat_session_id, user_id, role, content, metadata)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, user_id, role, content, json.dumps(metadata) if metadata else None),
    )
    await conn.execute(
        "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    await conn.commit()
    return cur.lastrowid


async def _store_generated_section(conn, session_id, user_id, section_type, content) -> int:
    cur = await conn.execute(
        """SELECT COALESCE(MAX(version), 0) AS v FROM generated_sections
           WHERE chat_session_id = ? AND user_id = ? AND section_type = ?""",
        (session_id, user_id, section_type),
    )
    row = await cur.fetchone()
    version = (row["v"] or 0) + 1
    await conn.execute(
        """INSERT INTO generated_sections (chat_session_id, user_id, section_type, version, content)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, user_id, section_type, version, content),
    )
    await conn.commit()
    return version


async def _maybe_set_title(conn, session_id, user_id, message: str) -> None:
    cur = await conn.execute(
        "SELECT title FROM chat_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    row = await cur.fetchone()
    if row and row["title"] == "New Session":
        # Prefer the extracted invention title if available.
        tcur = await conn.execute(
            """SELECT content_text FROM extracted_content
               WHERE chat_session_id = ? AND user_id = ? AND content_type = 'invention_title'
               LIMIT 1""",
            (session_id, user_id),
        )
        trow = await tcur.fetchone()
        title = None
        if trow and trow["content_text"] and trow["content_text"] != "NOT_PROVIDED_IN_IDF":
            title = trow["content_text"][:80]
        else:
            title = (message[:60] + "…") if len(message) > 60 else message
        await conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ? AND user_id = ?",
            (title, session_id, user_id),
        )
        await conn.commit()


async def stream_chat_turn(
    conn: aiosqlite.Connection,
    session_id: str,
    user_id: int,
    message: str,
    section_type: Optional[str],
) -> AsyncIterator[dict]:
    """
    Async generator yielding SSE event dicts:
      {"type": "meta", "section_type": ..., "blocked": bool, "used": [...]}
      {"type": "token", "content": "..."}
      {"type": "done", "message_id": int, "section_version": int|None}
      {"type": "error", "message": "..."}
    """
    # Resolve section: explicit from button click, else keyword detection.
    resolved_section = section_type or detect_section_type(message)

    await _maybe_set_title(conn, session_id, user_id, message)

    # Persist the user message up-front so it survives a mid-stream failure.
    await _store_message(
        conn, session_id, user_id, "user", message,
        metadata={"section_type": resolved_section},
    )

    ctx = await router.build_drafting_context(conn, session_id, user_id, resolved_section)

    yield {
        "type": "meta",
        "section_type": resolved_section,
        "blocked": ctx.blocked,
        "used": ctx.used_content_types,
    }

    # Dependency block (e.g. Technical Advantages before Technical Problems).
    if ctx.blocked:
        block_text = ctx.block_message
        for chunk in _chunk(block_text):
            yield {"type": "token", "content": chunk}
        msg_id = await _store_message(
            conn, session_id, user_id, "assistant", block_text,
            metadata={"section_type": resolved_section, "blocked": True},
        )
        yield {"type": "done", "message_id": msg_id, "section_version": None}
        return

    history = await _load_history(conn, session_id, user_id)
    # History already contains the just-stored user message (last item); it becomes
    # the final user turn. No need to append `message` again.
    api_messages = _normalize_messages(history)
    if not api_messages:
        api_messages = [{"role": "user", "content": message}]

    full_text_parts: list[str] = []
    try:
        async for delta in llm.stream_chat(
            system=ctx.system_prompt,
            messages=api_messages,
            max_tokens=config.MAX_TOKENS_DRAFTING,
        ):
            full_text_parts.append(delta)
            yield {"type": "token", "content": delta}
    except Exception as exc:
        # Persist whatever we have and surface the error.
        partial = "".join(full_text_parts)
        err = f"[Error contacting the model: {exc}]"
        assistant_text = (partial + "\n\n" + err) if partial else err
        msg_id = await _store_message(
            conn, session_id, user_id, "assistant", assistant_text,
            metadata={"section_type": resolved_section, "error": True},
        )
        yield {"type": "error", "message": str(exc)}
        yield {"type": "done", "message_id": msg_id, "section_version": None}
        return

    full_text = "".join(full_text_parts)
    msg_id = await _store_message(
        conn, session_id, user_id, "assistant", full_text,
        metadata={"section_type": resolved_section, "used": ctx.used_content_types},
    )

    # If a section was being drafted, store a new version.
    section_version = None
    if resolved_section in router.SECTION_TYPES and full_text.strip():
        section_version = await _store_generated_section(
            conn, session_id, user_id, resolved_section, full_text
        )

    yield {"type": "done", "message_id": msg_id, "section_version": section_version}


def _chunk(text: str, size: int = 60):
    for i in range(0, len(text), size):
        yield text[i:i + size]
