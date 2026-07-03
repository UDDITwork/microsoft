"""
The routing table: which extracted content + which instruction prompt feed each
drafted section, plus dependency enforcement.

`build_drafting_context` assembles the full SYSTEM PROMPT for a drafting turn and
reports whether the request is blocked by an unmet dependency.
"""
from dataclasses import dataclass, field
from typing import Optional

import aiosqlite

# Mandatory behavioural rules injected into EVERY drafting system prompt.
BEHAVIORAL_RULES = """BEHAVIORAL RULES — MANDATORY FOR EVERY RESPONSE:

1. ISOLATION: You are drafting for ONE specific invention in ONE specific chat session.
   Never reference, recall, or use content from any other session or invention.

2. CONTENT ROUTING: Use ONLY the extracted content provided in this system prompt.
   Do not invent, assume, or hallucinate any claim text, title, or technical content.

3. SECTION DEPENDENCY: If asked to write Technical Advantages but no Technical Problems
   section exists in the generated sections provided, REFUSE and ask the user to generate
   Technical Problems first.

4. EXTRACTION AWARENESS: You know what was extracted and what was not.
   If background was NOT_PROVIDED_IN_IDF, inform the user you will infer from claims.
   Never pretend to have information that was not extracted.

5. VERSION TRACKING: If the user asks you to revise a section, generate a new version.
   Reference the previous version in your response.

6. NEVER MIX SECTIONS: If the user is discussing Background, do not volunteer
   Technical Advantages content. Stay in the lane of the requested section.

7. CLAIM VERBATIM RULE: When the instruction prompt says to use claim text verbatim
   (as in Summary and Summary Paraphrasing), copy the exact claim text — do not
   paraphrase, reorder, or modify the claim language.

8. MEMORY: You have access to the full chat history of this session. Use it.
   If the user said "change X to Y" three messages ago, that change is still in effect."""

SECTION_TYPES = [
    "background",
    "summary",
    "technical_problems",
    "technical_advantages",
    "summary_paraphrasing",
    "brief_description_drawings",
]

SECTION_LABELS = {
    "background": "Background",
    "summary": "Summary",
    "technical_problems": "Technical Problems",
    "technical_advantages": "Technical Advantages",
    "summary_paraphrasing": "Summary Paraphrasing",
    "brief_description_drawings": "Brief Description of Drawings",
}


@dataclass
class DraftingContext:
    section_type: Optional[str]
    system_prompt: str
    blocked: bool = False
    block_message: str = ""
    used_content_types: list[str] = field(default_factory=list)


# --- DB helpers --------------------------------------------------------------

async def _get_instruction_prompt(conn: aiosqlite.Connection, section_type: str) -> str:
    cur = await conn.execute(
        "SELECT system_prompt FROM instruction_prompts WHERE section_type = ?",
        (section_type,),
    )
    row = await cur.fetchone()
    return row["system_prompt"] if row else ""


async def _get_extracted(conn, session_id, user_id, content_type: str) -> Optional[str]:
    cur = await conn.execute(
        """SELECT content_text FROM extracted_content
           WHERE chat_session_id = ? AND user_id = ? AND content_type = ?
           ORDER BY id ASC LIMIT 1""",
        (session_id, user_id, content_type),
    )
    row = await cur.fetchone()
    return row["content_text"] if row else None


async def _get_all_dependent_claims(conn, session_id, user_id) -> list[str]:
    cur = await conn.execute(
        """SELECT content_text FROM extracted_content
           WHERE chat_session_id = ? AND user_id = ? AND content_type = 'claim_dependent'
           ORDER BY claim_number ASC""",
        (session_id, user_id),
    )
    rows = await cur.fetchall()
    return [r["content_text"] for r in rows]


async def _get_latest_generated(conn, session_id, user_id, section_type: str) -> Optional[str]:
    cur = await conn.execute(
        """SELECT content, version FROM generated_sections
           WHERE chat_session_id = ? AND user_id = ? AND section_type = ?
           ORDER BY version DESC LIMIT 1""",
        (session_id, user_id, section_type),
    )
    row = await cur.fetchone()
    return row["content"] if row else None


async def _list_generated_sections(conn, session_id, user_id) -> list[dict]:
    cur = await conn.execute(
        """SELECT section_type, MAX(version) AS version, content
           FROM generated_sections
           WHERE chat_session_id = ? AND user_id = ?
           GROUP BY section_type
           ORDER BY MAX(generated_at)""",
        (session_id, user_id),
    )
    rows = await cur.fetchall()
    return [{"section_type": r["section_type"], "version": r["version"], "content": r["content"]} for r in rows]


# --- Context assembly --------------------------------------------------------

def _fmt(label: str, value: Optional[str]) -> str:
    if value is None:
        return f"{label}: [NOT EXTRACTED]\n\n"
    return f"{label}:\n{value}\n\n"


async def build_drafting_context(
    conn: aiosqlite.Connection,
    session_id: str,
    user_id: int,
    section_type: Optional[str],
) -> DraftingContext:
    """
    Assemble the drafting system prompt for `section_type`. When section_type is
    None (free-form chat), a general context is built with all extracted content
    and generated sections so the agent can answer "what have we done so far?".
    """
    title = await _get_extracted(conn, session_id, user_id, "invention_title")
    claim_1 = await _get_extracted(conn, session_id, user_id, "claim_independent_1")
    generated = await _list_generated_sections(conn, session_id, user_id)
    used: list[str] = []

    # Instruction prompt for the requested section (empty for free-form chat)
    instruction = ""
    if section_type in SECTION_TYPES:
        instruction = await _get_instruction_prompt(conn, section_type)

    content_blocks: list[str] = []
    blocked = False
    block_message = ""

    if section_type == "background":
        bg = await _get_extracted(conn, session_id, user_id, "background")
        content_blocks.append(_fmt("INDEPENDENT CLAIM 1 (words/phrases to AVOID revealing)", claim_1))
        content_blocks.append(_fmt("INVENTION TITLE (words to AVOID in background)", title))
        if bg and bg != "NOT_PROVIDED_IN_IDF":
            content_blocks.append(_fmt("BACKGROUND FACTS FROM IDF (use facts ONLY, not style)", bg))
        else:
            content_blocks.append("BACKGROUND FROM IDF: NOT_PROVIDED_IN_IDF — infer general field context from Claim 1; tell the user you are inferring.\n\n")
        used = ["claim_independent_1", "invention_title", "background"]

    elif section_type == "summary":
        content_blocks.append(_fmt("INDEPENDENT CLAIM 1 (VERBATIM SOURCE for Summary paragraph 1)", claim_1))
        content_blocks.append(_fmt("INVENTION TITLE", title))
        content_blocks.append(
            "IMPORTANT: The Summary uses Claim 1 VERBATIM for paragraph 1, then fixed boilerplate "
            "for paragraphs 2 and 3. Do NOT use IDF prose for the Summary.\n\n"
        )
        used = ["claim_independent_1", "invention_title"]

    elif section_type == "technical_problems":
        tp = await _get_extracted(conn, session_id, user_id, "technical_problems")
        content_blocks.append(_fmt("INDEPENDENT CLAIM 1 (to understand scope — NEVER reveal the invention in problems)", claim_1))
        content_blocks.append(_fmt("INVENTION TITLE (handle carefully — do not reveal solution)", title))
        if tp and tp != "NOT_PROVIDED_IN_IDF":
            content_blocks.append(_fmt("TECHNICAL PROBLEMS FROM IDF", tp))
        else:
            content_blocks.append("TECHNICAL PROBLEMS FROM IDF: NOT_PROVIDED_IN_IDF — infer plausible problems from the field/Claim 1; tell the user you are inferring.\n\n")
        used = ["claim_independent_1", "invention_title", "technical_problems"]

    elif section_type == "technical_advantages":
        # DEPENDENCY: Technical Problems section must already be generated.
        tp_section = await _get_latest_generated(conn, session_id, user_id, "technical_problems")
        if tp_section is None:
            blocked = True
            block_message = (
                "Technical Advantages responds to Technical Problems. "
                "Please generate Technical Problems first."
            )
        else:
            dependents = await _get_all_dependent_claims(conn, session_id, user_id)
            content_blocks.append(_fmt("INDEPENDENT CLAIM 1 (primary — shows HOW invention solves problems)", claim_1))
            if dependents:
                joined = "\n\n".join(dependents)
                content_blocks.append(_fmt("IMPORTANT DEPENDENT CLAIMS", joined))
            content_blocks.append(_fmt("ALREADY-GENERATED TECHNICAL PROBLEMS (advantages must respond to these)", tp_section))
            content_blocks.append(_fmt("INVENTION TITLE", title))
            used = ["claim_independent_1", "claim_dependent", "generated:technical_problems", "invention_title"]

    elif section_type == "summary_paraphrasing":
        all_claims = await _get_extracted(conn, session_id, user_id, "all_claims_raw")
        claim_system = await _get_extracted(conn, session_id, user_id, "claim_independent_system")
        claim_cpp = await _get_extracted(conn, session_id, user_id, "claim_independent_cpp")
        content_blocks.append(_fmt("ALL CLAIMS (VERBATIM — independent AND dependent)", all_claims))
        content_blocks.append(_fmt("INDEPENDENT SYSTEM CLAIM", claim_system))
        content_blocks.append(_fmt("INDEPENDENT COMPUTER-PROGRAM-PRODUCT CLAIM", claim_cpp))
        content_blocks.append(_fmt("INVENTION TITLE", title))
        used = ["all_claims_raw", "claim_independent_system", "claim_independent_cpp", "invention_title"]

    elif section_type == "brief_description_drawings":
        figures = await _get_extracted(conn, session_id, user_id, "figure_descriptions")
        content_blocks.append(_fmt("INVENTION TITLE", title))
        if figures and figures != "NOT_PROVIDED_IN_IDF":
            content_blocks.append(_fmt("FIGURE DESCRIPTIONS FROM IDF", figures))
        else:
            content_blocks.append("FIGURE DESCRIPTIONS FROM IDF: NOT_PROVIDED_IN_IDF — inform the user no figures were found.\n\n")
        used = ["invention_title", "figure_descriptions"]

    else:
        # Free-form chat: provide title, claim 1, and an inventory of what exists.
        content_blocks.append(_fmt("INVENTION TITLE", title))
        content_blocks.append(_fmt("INDEPENDENT CLAIM 1", claim_1))
        used = ["invention_title", "claim_independent_1"]

    # --- Assemble the system prompt -----------------------------------------
    parts: list[str] = [BEHAVIORAL_RULES, "\n\n"]
    if instruction:
        parts.append("DRAFTING INSTRUCTIONS FOR THIS SECTION:\n")
        parts.append(instruction)
        parts.append("\n\n")
    parts.append("---\n\nEXTRACTED CONTENT FOR THIS SESSION:\n\n")
    parts.append(_fmt("INVENTION TITLE", title))
    if claim_1:
        parts.append(_fmt("INDEPENDENT CLAIM 1", claim_1))
    parts.append("".join(content_blocks))
    parts.append("---\n\nPREVIOUSLY GENERATED SECTIONS IN THIS SESSION:\n")
    if generated:
        for g in generated:
            parts.append(f"\n[{SECTION_LABELS.get(g['section_type'], g['section_type'])} — v{g['version']}]\n{g['content']}\n")
    else:
        parts.append("(none yet)\n")

    return DraftingContext(
        section_type=section_type,
        system_prompt="".join(parts),
        blocked=blocked,
        block_message=block_message,
        used_content_types=used,
    )
