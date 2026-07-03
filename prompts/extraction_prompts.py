"""
Hardcoded prompts for the DOCUMENT EXTRACTION phase (parsing, not drafting).

These are deliberately deterministic/parsing-oriented and live in code (not the
DB) because they are infrastructure, not creative drafting instructions.
"""

CLAIMS_EXTRACTION_PROMPT = """You are a patent claims parser. Given the following document text, extract every patent claim.

For each claim, return a JSON object with:
- "claim_number": integer
- "claim_type": "independent" or "dependent"
- "claim_category": "method" or "system" or "computer_program_product" (for independent claims only, null for dependent)
- "depends_on": integer or null (which claim this depends on, null for independent)
- "full_text": the complete verbatim text of the claim

Return a JSON array of these objects. Nothing else — no markdown, no explanation, only valid JSON.

DOCUMENT TEXT:
---
{document_text}
---"""

TITLE_EXTRACTION_PROMPT = """You are a patent document parser. Given the following Invention Disclosure Form (IDF) text, extract the invention title.

The title is usually found near the top of the document, often after "Title:" or "Invention Title:" or as the first heading.

Return ONLY the title text, nothing else. No quotes, no explanation.

IDF TEXT:
---
{document_text}
---"""

BACKGROUND_EXTRACTION_PROMPT = """You are a patent document parser. Given the following IDF text, determine if there is a "Background", "Prior Art", "Related Work", or "State of the Art" section.

If such a section exists:
- Extract ONLY the factual technical statements about the existing state of the field
- Do NOT extract opinions, value judgments, or problem statements
- Do NOT extract anything that sounds like "the problem is..." or "existing systems fail to..."
- Extract plain facts: what exists, how things generally work, what technologies are used

Return the extracted facts as plain text paragraphs. If no background section exists, return exactly: NOT_PROVIDED_IN_IDF

IDF TEXT:
---
{document_text}
---"""

TECHNICAL_PROBLEMS_EXTRACTION_PROMPT = """You are a patent document parser. Given the following IDF text, determine if the inventor has described any problems, challenges, limitations, or shortcomings of existing approaches.

If such content exists:
- Extract the technical problems/challenges described
- Keep them as factual statements
- Do NOT add your own analysis

Return the extracted problems as plain text. If no problems section exists, return exactly: NOT_PROVIDED_IN_IDF

IDF TEXT:
---
{document_text}
---"""

FIGURE_DESCRIPTIONS_EXTRACTION_PROMPT = """You are a patent document parser. Given the following IDF text, extract any figure descriptions, captions, or references.

Look for patterns like:
- "Figure 1 shows..."
- "FIG. 2 depicts..."
- "As shown in Figure 3..."
- Figure titles or captions

Return each figure description on a new line in format:
FIG. [N]: [description]

If no figure descriptions are found, return exactly: NOT_PROVIDED_IN_IDF

IDF TEXT:
---
{document_text}
---"""

INVENTOR_NAMES_EXTRACTION_PROMPT = """You are a patent document parser. Given the following IDF text, extract the inventor name(s) if present.

Look for fields like "Inventor(s):", "Inventor Name:", "Submitted by:", or a list of names near the top of the document.

Return the inventor names as a comma-separated list on a single line. If no inventor names are found, return exactly: NOT_PROVIDED_IN_IDF

IDF TEXT:
---
{document_text}
---"""
