"""
End-to-end smoke test (LLM layer mocked — no Anthropic API key needed).

Storage runs against the real Turso database (TURSO_DATABASE_URL / TURSO_AUTH_TOKEN
must be set in the environment). The test is written to be SAFE against a shared
production database:
  * identities are randomized per run (no UNIQUE-username collisions across runs),
  * the shared `background` instruction prompt is snapshotted and restored around
    the prompt-update check (never leaves prod mutated),
  * every user / session / document / message / section it creates is deleted in a
    cleanup pass at the end (runs even on failure).

Run locally:   (set TURSO_* + JWT_SECRET, then)  python tests/test_smoke.py
Run in CI:     see .github/workflows/ci-cd.yml (Turso secrets injected)

Validates: boot, DB init, auth, user + session isolation, upload, doc-type
detection, extraction persistence, routing, dependency enforcement (Technical
Advantages before Technical Problems), streaming chat, and section versioning.
"""
import io
import os
import sys
import json
import uuid
import asyncio
import tempfile
from pathlib import Path

# --- Env BEFORE importing the app --------------------------------------------
# Uploaded-file copies go to a throwaway local dir (local disk is fine for the
# .docx originals — the source of truth is the parsed text stored in Turso).
_tmp = tempfile.mkdtemp()
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.makedirs(os.environ["UPLOAD_DIR"], exist_ok=True)
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")

if not os.environ.get("TURSO_DATABASE_URL") or not os.environ.get("TURSO_AUTH_TOKEN"):
    print("FAIL - TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set for the smoke test.")
    sys.exit(1)

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, PROJECT_ROOT)

from docx import Document
from services import llm

# Unique per-run identities so repeated CI runs never collide on UNIQUE(username).
RUN = uuid.uuid4().hex[:10]
USER_A = f"alice_{RUN}"
USER_B = f"bob_{RUN}"


# --- Mock the LLM layer ------------------------------------------------------
async def fake_complete(system, user_content, *, max_tokens=8000):
    if "patent claims parser" in system or "valid JSON" in system:
        return json.dumps([
            {"claim_number": 1, "claim_type": "independent", "claim_category": "method",
             "depends_on": None, "full_text": "1. A computer-implemented method comprising: doing X."},
            {"claim_number": 2, "claim_type": "dependent", "claim_category": None,
             "depends_on": 1, "full_text": "2. The method of claim 1, wherein X uses Y."},
            {"claim_number": 3, "claim_type": "independent", "claim_category": "system",
             "depends_on": None, "full_text": "3. A system comprising a processor configured to do X."},
            {"claim_number": 4, "claim_type": "independent", "claim_category": "computer_program_product",
             "depends_on": None, "full_text": "4. A computer program product storing instructions to do X."},
        ])
    prompt = user_content.lower()
    if "invention title" in prompt:
        return "Priority-Based Cooperative Live Migration of Training Jobs"
    if "background" in prompt or "state of the art" in prompt:
        return "Existing schedulers place training jobs on GPUs using bin-packing."
    if "problems" in prompt or "challenges" in prompt:
        return "NOT_PROVIDED_IN_IDF"
    if "figure" in prompt:
        return "FIG. 1: system overview"
    if "inventor" in prompt:
        return "Jane Doe, John Smith"
    return "NOT_PROVIDED_IN_IDF"


async def fake_stream_chat(system, messages, *, max_tokens=8000):
    for tok in ["Drafted ", "section ", "content ", "based ", "on ", "Claim 1."]:
        yield tok


llm.complete = fake_complete
llm.stream_chat = fake_stream_chat

from fastapi.testclient import TestClient
import database
import main

asyncio.run(database.init_db())
client = TestClient(main.app)


def make_docx(paragraphs):
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


r = client.get("/api/health")
check("health ok", r.status_code == 200 and r.json()["status"] == "ok")

ra = client.post("/api/auth/register", json={"username": USER_A, "password": "secret1"})
rb = client.post("/api/auth/register", json={"username": USER_B, "password": "secret2"})
check("register alice", ra.status_code == 200)
check("register bob", rb.status_code == 200)
tok_a = ra.json()["access_token"]
tok_b = rb.json()["access_token"]

check("duplicate username rejected",
      client.post("/api/auth/register", json={"username": USER_A, "password": "x123456"}).status_code == 409)
check("wrong password rejected",
      client.post("/api/auth/login", json={"username": USER_A, "password": "nope"}).status_code == 401)

rs = client.post("/api/sessions", json={}, headers=hdr(tok_a))
check("create session", rs.status_code == 200)
sid = rs.json()["id"]

check("bob sees no sessions", len(client.get("/api/sessions", headers=hdr(tok_b)).json()) == 0)
check("bob 404 on alice session", client.get(f"/api/sessions/{sid}", headers=hdr(tok_b)).status_code == 404)
check("bob 404 chat on alice session",
      client.post(f"/api/sessions/{sid}/chat", json={"message": "hi"}, headers=hdr(tok_b)).status_code == 404)

claims_docx = make_docx(["CLAIMS", "1. A computer-implemented method comprising: doing X.",
                         "2. The method of claim 1, wherein X uses Y."])
idf_docx = make_docx(["INVENTION DISCLOSURE FORM",
                      "Title: Priority-Based Cooperative Live Migration of Training Jobs",
                      "Description of Invention: The system migrates training jobs. Figure 1 shows the overview."])
files = [
    ("files", ("claims.docx", claims_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
    ("files", ("idf.docx", idf_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
]
rup = client.post(f"/api/sessions/{sid}/upload", files=files, headers=hdr(tok_a))
check("upload 2 docs", rup.status_code == 200 and len(rup.json()["documents"]) == 2)

docs = client.get(f"/api/sessions/{sid}/documents", headers=hdr(tok_a)).json()
types = sorted(d["doc_type"] for d in docs)
check("doc types detected (claims+idf)", "claims" in types and "idf" in types)

check("3rd upload rejected",
      client.post(f"/api/sessions/{sid}/upload",
                  files=[("files", ("extra.docx", claims_docx, "application/octet-stream"))],
                  headers=hdr(tok_a)).status_code == 400)
check("non-docx rejected",
      client.post(f"/api/sessions/{sid}/upload",
                  files=[("files", ("note.txt", b"hello", "text/plain"))],
                  headers=hdr(tok_a)).status_code == 400)

with client.stream("POST", f"/api/sessions/{sid}/extract", headers=hdr(tok_a)) as resp:
    events = [json.loads(line[5:].strip()) for line in resp.iter_lines() if line and line.startswith("data:")]
complete = next((e for e in events if e["type"] == "complete"), None)
check("extraction streamed complete", complete is not None)
check("extraction summary mentions claims", complete and "claims" in complete["summary"].lower())

ext = client.get(f"/api/sessions/{sid}/extracted", headers=hdr(tok_a)).json()
ctypes = {e["content_type"] for e in ext}
for ct in ["claim_independent_1", "claim_independent_system", "claim_independent_cpp",
           "claim_dependent", "all_claims_raw", "invention_title"]:
    check(f"{ct} stored", ct in ctypes)
title = next((e["content_text"] for e in ext if e["content_type"] == "invention_title"), "")
check("title correct", "Priority-Based" in title)
check("bob 404 on extracted", client.get(f"/api/sessions/{sid}/extracted", headers=hdr(tok_b)).status_code == 404)


def do_chat(section_type=None, message="hi"):
    payload = {"message": message}
    if section_type:
        payload["section_type"] = section_type
    toks, done = "", None
    with client.stream("POST", f"/api/sessions/{sid}/chat", json=payload, headers=hdr(tok_a)) as resp:
        for line in resp.iter_lines():
            if line and line.startswith("data:"):
                ev = json.loads(line[5:].strip())
                if ev["type"] == "token":
                    toks += ev["content"]
                elif ev["type"] == "done":
                    done = ev
    return toks, done


toks, done = do_chat("technical_advantages", "Generate the Technical Advantages section.")
check("tech advantages blocked without problems",
      "Technical Problems first" in toks and (done is None or done.get("section_version") is None))

toks, done = do_chat("technical_problems", "Generate the Technical Problems section.")
check("tech problems generated v1", done and done.get("section_version") == 1)

toks, done = do_chat("technical_advantages", "Generate the Technical Advantages section.")
check("tech advantages now generated", done and done.get("section_version") == 1 and "Drafted" in toks)

toks, done = do_chat("background", "Generate the Background section.")
check("background v1", done and done.get("section_version") == 1)
toks, done = do_chat("background", "Revise: shorten it.")
check("background v2 on revise", done and done.get("section_version") == 2)

secs = {s["section_type"]: s["version"] for s in client.get(f"/api/sessions/{sid}/sections", headers=hdr(tok_a)).json()}
check("sections list has background v2", secs.get("background") == 2)
check("sections list has technical_problems", "technical_problems" in secs)

check("messages persisted", len(client.get(f"/api/sessions/{sid}/messages", headers=hdr(tok_a)).json()) >= 8)

prompts = client.get("/api/prompts", headers=hdr(tok_a)).json()
check("6 instruction prompts seeded", len(prompts) == 6)
# instruction_prompts is a SHARED/global table — snapshot the real background
# prompt so we can restore it after mutating (never leave prod changed).
_orig_bg = next((p["system_prompt"] for p in prompts if p["section_type"] == "background"), None)
pu = client.put("/api/prompts/background", json={"system_prompt": "NEW BACKGROUND PROMPT"}, headers=hdr(tok_a))
check("update prompt", pu.status_code == 200 and pu.json()["system_prompt"] == "NEW BACKGROUND PROMPT")
if _orig_bg is not None:
    rr = client.put("/api/prompts/background", json={"system_prompt": _orig_bg}, headers=hdr(tok_a))
    check("background prompt restored", rr.status_code == 200 and rr.json()["system_prompt"] == _orig_bg)

sid2 = client.post("/api/sessions", json={}, headers=hdr(tok_b)).json()["id"]
check("bob new session has no extracted content",
      len(client.get(f"/api/sessions/{sid2}/extracted", headers=hdr(tok_b)).json()) == 0)

check("unauth blocked", client.get("/api/sessions").status_code == 401)


# --- Cleanup: remove everything this run created from the shared Turso DB -----
async def _cleanup():
    conn = await database.get_db()
    try:
        cur = await conn.execute(
            "SELECT id FROM users WHERE username IN (?, ?)", (USER_A, USER_B)
        )
        uids = [r["id"] for r in await cur.fetchall()]
        for uid in uids:
            # Delete children explicitly (don't rely on cascade), then session, then user.
            for tbl in ("extracted_content", "chat_messages", "generated_sections",
                        "uploaded_documents", "chat_sessions"):
                await conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (uid,))
            await conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        await conn.commit()
        return len(uids)
    finally:
        await conn.close()


try:
    n_cleaned = asyncio.run(_cleanup())
    check("cleanup removed test users", n_cleaned == 2)
except Exception as exc:  # noqa: BLE001
    check(f"cleanup removed test users (error: {exc})", False)


print("\n==== SUMMARY ====")
passed = sum(1 for _, c in results if c)
print(f"{passed}/{len(results)} checks passed")
fails = [n for n, c in results if not c]
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")
