"""
Semantic extraction pipeline (Step 2 of the spec).

Runs the six extractions concurrently (network-bound Claude calls), persists each
result into extracted_content, and yields progress events for SSE streaming.

All writes are scoped to (chat_session_id, user_id).
"""
import asyncio
import json
import re
from typing import AsyncIterator, Optional

import database

from prompts import extraction_prompts as ep
from services import llm

# Truncate very long documents before sending to the model (safety ceiling).
MAX_DOC_CHARS = 120_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # remove leading ```json / ``` and trailing ```
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _parse_claims_json(raw: str) -> list[dict]:
    """Best-effort parse of the claims JSON array from the model output."""
    cleaned = _strip_code_fences(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back: grab the outermost [...] block
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = [data]
    return data if isinstance(data, list) else []


def _classify_independent(category: Optional[str]) -> str:
    """Map an independent claim's category to its content_type."""
    cat = (category or "").lower()
    if "system" in cat:
        return "claim_independent_system"
    if "computer" in cat or "cpp" in cat or "program" in cat:
        return "claim_independent_cpp"
    # default: method-style independent claim
    return "claim_independent_1"


# ---------------------------------------------------------------------------
# Individual extractions (each returns rows: list of (content_type, text, claim_no, metadata))
# ---------------------------------------------------------------------------

async def _extract_claims(claims_text: str) -> tuple[list[tuple], dict]:
    raw = await llm.complete(
        system="You output only valid JSON as instructed.",
        user_content=ep.CLAIMS_EXTRACTION_PROMPT.format(document_text=claims_text[:MAX_DOC_CHARS]),
    )
    claims = _parse_claims_json(raw)
    rows: list[tuple] = []
    n_independent = 0
    n_dependent = 0

    # Guard: keep only the FIRST independent of each category (spec content_types
    # are singular for independents). Extra independents of same category are
    # stored as all_claims_raw only.
    seen_independent_types: set[str] = set()

    for c in claims:
        if not isinstance(c, dict):
            continue
        full_text = str(c.get("full_text", "")).strip()
        if not full_text:
            continue
        claim_number = c.get("claim_number")
        claim_type = str(c.get("claim_type", "")).lower()
        meta = json.dumps({
            "claim_number": claim_number,
            "claim_type": claim_type,
            "claim_category": c.get("claim_category"),
            "depends_on": c.get("depends_on"),
        })

        if claim_type == "independent":
            n_independent += 1
            ctype = _classify_independent(c.get("claim_category"))
            if ctype in seen_independent_types:
                # Secondary independent of an already-seen category: keep as raw only.
                continue
            seen_independent_types.add(ctype)
            rows.append((ctype, full_text, claim_number, meta))
        else:
            n_dependent += 1
            rows.append(("claim_dependent", full_text, claim_number, meta))

    # Whole claims blob
    all_raw = "\n\n".join(
        str(c.get("full_text", "")).strip() for c in claims if isinstance(c, dict)
    ).strip()
    if all_raw:
        rows.append(("all_claims_raw", all_raw, None, json.dumps({"total_claims": len(claims)})))

    stats = {
        "total": len([c for c in claims if isinstance(c, dict) and str(c.get("full_text", "")).strip()]),
        "independent": n_independent,
        "dependent": n_dependent,
    }
    return rows, stats


async def _extract_simple(prompt_template: str, source_text: str, content_type: str) -> tuple[list[tuple], dict]:
    """Generic single-value extraction (title, background, problems, figures, inventors)."""
    result = await llm.complete(
        system="You are a precise patent document parser.",
        user_content=prompt_template.format(document_text=source_text[:MAX_DOC_CHARS]),
    )
    result = result.strip()
    if not result:
        result = "NOT_PROVIDED_IN_IDF"
    found = result != "NOT_PROVIDED_IN_IDF"
    return [(content_type, result, None, None)], {"found": found, "value": result}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _clear_previous(conn: database.Connection, session_id: str, user_id: int) -> None:
    await conn.execute(
        "DELETE FROM extracted_content WHERE chat_session_id = ? AND user_id = ?",
        (session_id, user_id),
    )


async def _insert_rows(conn: database.Connection, session_id: str, user_id: int, rows: list[tuple]) -> None:
    for content_type, text, claim_no, meta in rows:
        await conn.execute(
            """INSERT INTO extracted_content
               (chat_session_id, user_id, content_type, content_text, claim_number, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, user_id, content_type, text, claim_no, meta),
        )


async def _select_sources(conn: database.Connection, session_id: str, user_id: int) -> tuple[str, str]:
    """Return (claims_source_text, idf_source_text) from uploaded docs."""
    cur = await conn.execute(
        "SELECT doc_type, raw_text FROM uploaded_documents WHERE chat_session_id = ? AND user_id = ?",
        (session_id, user_id),
    )
    docs = await cur.fetchall()
    all_text = "\n\n".join(d["raw_text"] for d in docs)
    claims_doc = next((d["raw_text"] for d in docs if d["doc_type"] == "claims"), None)
    idf_doc = next((d["raw_text"] for d in docs if d["doc_type"] == "idf"), None)
    # Fall back to combined text when a role wasn't confidently detected.
    return (claims_doc or all_text, idf_doc or all_text)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_extraction_pipeline(
    conn: database.Connection, session_id: str, user_id: int
) -> AsyncIterator[dict]:
    """
    Async generator yielding progress events. Persists results as they complete.

    Event shapes:
      {"type": "start", "steps": [...]}
      {"type": "progress", "step": <key>, "message": <str>, "found": <bool?>}
      {"type": "complete", "summary": <str>, "counts": {...}}
      {"type": "error", "message": <str>}
    """
    claims_text, idf_text = await _select_sources(conn, session_id, user_id)

    await conn.execute(
        "UPDATE chat_sessions SET extraction_status = 'in_progress' WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    await conn.commit()

    yield {
        "type": "start",
        "steps": ["claims", "title", "background", "technical_problems", "figure_descriptions", "inventor_names"],
    }

    # Build named tasks so as_completed can report which finished.
    async def named(key, coro):
        return key, await coro

    tasks = {
        "claims": asyncio.create_task(named("claims", _extract_claims(claims_text))),
        "title": asyncio.create_task(named("title", _extract_simple(ep.TITLE_EXTRACTION_PROMPT, idf_text, "invention_title"))),
        "background": asyncio.create_task(named("background", _extract_simple(ep.BACKGROUND_EXTRACTION_PROMPT, idf_text, "background"))),
        "technical_problems": asyncio.create_task(named("technical_problems", _extract_simple(ep.TECHNICAL_PROBLEMS_EXTRACTION_PROMPT, idf_text, "technical_problems"))),
        "figure_descriptions": asyncio.create_task(named("figure_descriptions", _extract_simple(ep.FIGURE_DESCRIPTIONS_EXTRACTION_PROMPT, idf_text, "figure_descriptions"))),
        "inventor_names": asyncio.create_task(named("inventor_names", _extract_simple(ep.INVENTOR_NAMES_EXTRACTION_PROMPT, idf_text, "inventor_names"))),
    }

    await _clear_previous(conn, session_id, user_id)

    counts = {"claims_total": 0, "claims_independent": 0, "claims_dependent": 0}
    summary_bits: dict[str, object] = {}
    had_error = False

    try:
        for finished in asyncio.as_completed(list(tasks.values())):
            try:
                key, (rows, stats) = await finished
            except Exception as exc:  # a single extraction failed
                had_error = True
                yield {"type": "progress", "step": "unknown", "message": f"✗ An extraction step failed: {exc}"}
                continue

            await _insert_rows(conn, session_id, user_id, rows)
            await conn.commit()

            if key == "claims":
                counts["claims_total"] = stats["total"]
                counts["claims_independent"] = stats["independent"]
                counts["claims_dependent"] = stats["dependent"]
                summary_bits["claims"] = stats
                msg = f"✓ Claims extracted ({stats['total']} claims found: {stats['independent']} independent, {stats['dependent']} dependent)"
                if stats["total"] == 0:
                    msg = "⚠ No claims found in uploaded documents. Please verify you uploaded the claims document."
                yield {"type": "progress", "step": "claims", "message": msg, "found": stats["total"] > 0}
            else:
                found = stats.get("found", True)
                summary_bits[key] = stats
                label = {
                    "title": "Title",
                    "background": "Background",
                    "technical_problems": "Technical problems",
                    "figure_descriptions": "Figure descriptions",
                    "inventor_names": "Inventor names",
                }[key]
                if key == "title":
                    val = stats.get("value", "")
                    yield {"type": "progress", "step": key, "message": f"✓ Title extracted: '{val}'", "found": True}
                else:
                    state = "found in IDF" if found else "not found in IDF"
                    yield {"type": "progress", "step": key, "message": f"✓ {label}: {state}", "found": found}
    except Exception as exc:
        had_error = True
        yield {"type": "error", "message": f"Extraction pipeline error: {exc}"}

    status = "error" if had_error else "complete"
    await conn.execute(
        "UPDATE chat_sessions SET extraction_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        (status, session_id, user_id),
    )
    await conn.commit()

    title = summary_bits.get("title", {}).get("value", "Unknown") if isinstance(summary_bits.get("title"), dict) else "Unknown"
    bg_found = summary_bits.get("background", {}).get("found", False)
    tp_found = summary_bits.get("technical_problems", {}).get("found", False)
    summary = (
        f"Extraction complete. Found {counts['claims_total']} claims "
        f"({counts['claims_independent']} independent, {counts['claims_dependent']} dependent), "
        f"title: '{title}', background: {'found' if bg_found else 'not found'} in IDF, "
        f"technical problems: {'found' if tp_found else 'not found'} in IDF."
    )
    yield {"type": "complete", "summary": summary, "counts": counts}
