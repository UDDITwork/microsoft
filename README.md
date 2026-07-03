# Patent Specification Drafting Assistant

A full-stack RAG assistant that helps patent professionals draft specification
sections from an Invention Disclosure Form (IDF) and a Claims document. It has two
phases: **(1) Document Upload & Extraction** and **(2) Section-by-Section Drafting
via chat**. Every piece of data is isolated per **user** and per **chat session** —
User A never sees User B's data, and Session 1 never leaks into Session 2.

- **Backend:** Python FastAPI + async SQLite (`aiosqlite`)
- **AI:** Anthropic Claude (configurable model, streaming)
- **Frontend:** single self-contained SPA (`static/index.html`, vanilla JS)
- **Deploy:** Docker → Google Cloud Run

---

## Architecture

```
main.py                     FastAPI app: routers, CORS, static, DB init
config.py                   Env-driven configuration
database.py                 Schema, indexes, seeding, per-request connection dep
auth.py                     bcrypt hashing + JWT + get_current_user dependency
models.py                   Pydantic request/response models
routers/
  auth_router.py            /api/auth/*         register / login / logout
  session_router.py         /api/sessions/*     session CRUD
  upload_router.py          upload + extraction SSE trigger
  chat_router.py            /api/sessions/{id}/chat  (SSE) + messages
  extraction_router.py      extracted-content reads
  sections_router.py        generated-section reads
  prompts_router.py         instruction-prompt admin (hot-swappable)
services/
  document_parser.py        python-docx text extraction + doc-type detection
  extractor.py              6 semantic extractions (concurrent) + SSE progress
  section_router_logic.py   ROUTING TABLE: content -> section + dependency checks
  chat_service.py           context assembly, streaming, message + version storage
  llm.py                    Anthropic client wrapper (retry + streaming)
prompts/extraction_prompts.py   hardcoded parsing prompts
static/index.html           two-panel SPA (auth, sessions, upload, chat, streaming)
```

### Data isolation
Every domain table carries both `chat_session_id` and `user_id`. Composite indexes
`(chat_session_id, user_id)` back every read, and every route filters on the
authenticated `user_id`. `session_owned_by()` guards every session-scoped endpoint.

### The routing table (the "intelligence")
`services/section_router_logic.py` maps each requested section to the exact
extracted content + instruction prompt it may use, and enforces dependencies:

| Request | Extracted content used | Notes |
|---|---|---|
| Background | Claim 1 + title (words to AVOID) + IDF background facts | never reveal the invention |
| Summary | Claim 1 (verbatim) + title | **does NOT use IDF prose** |
| Technical Problems | Claim 1 (scope) + title + IDF problems | never reveal the invention |
| Technical Advantages | Claim 1 + dependent claims + **generated Technical Problems** + title | **requires Technical Problems generated first** |
| Summary Paraphrasing | all_claims_raw + system claim + CPP claim + title | verbatim claims |
| Brief Description of Drawings | title + figure descriptions | |

The mandatory **behavioural rules** block is injected into every drafting system
prompt (isolation, content routing, dependency enforcement, extraction awareness,
version tracking, no section-mixing, claim-verbatim, memory).

---

## Run locally

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit ANTHROPIC_API_KEY + JWT_SECRET
export ANTHROPIC_API_KEY=sk-ant-...      # or set via .env / your shell
export JWT_SECRET=$(python -c "import secrets;print(secrets.token_hex(32))")

uvicorn main:app --host 0.0.0.0 --port 8080
```

Open <http://localhost:8080>, register, create a session, drop in your IDF +
Claims `.docx` files, watch extraction stream, then click the section buttons.

> **Model id:** the spec requested `claude-sonnet-4-6`. If that id is not available
> to your account, set `ANTHROPIC_MODEL=claude-sonnet-5` (or another current id).

---

## Docker

```bash
docker build -t patent-drafter .
docker run -p 8080:8080 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e JWT_SECRET=$(python -c "import secrets;print(secrets.token_hex(32))") \
  -v $(pwd)/data:/data \
  patent-drafter
```

## Google Cloud Run

```bash
gcloud builds submit --tag gcr.io/PROJECT_ID/patent-drafter
gcloud run deploy patent-drafter \
  --image gcr.io/PROJECT_ID/patent-drafter \
  --region us-central1 --platform managed --allow-unauthenticated \
  --memory 1Gi --cpu 1 --max-instances 1 --min-instances 0 --timeout 300 \
  --set-env-vars ANTHROPIC_API_KEY=sk-ant-...,JWT_SECRET=...,ANTHROPIC_MODEL=claude-sonnet-5
```

**Persistence:** SQLite lives at `/data/patent_drafter.db`. On Cloud Run, mount a
Cloud Storage FUSE volume (or persistent disk) at `/data`, otherwise the DB and
uploads are lost on container restart. Keep **`--max-instances 1`** for SQLite —
multiple instances would each get their own database file. For real multi-instance
production, migrate to Cloud SQL (PostgreSQL).

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/register` `/login` `/logout` | auth (JWT) |
| GET/POST | `/api/sessions` | list / create sessions |
| GET/DELETE | `/api/sessions/{id}` | details / archive |
| POST | `/api/sessions/{id}/upload` | upload up to 2 `.docx` |
| GET | `/api/sessions/{id}/documents` | list documents |
| POST | `/api/sessions/{id}/extract` | run extraction (SSE progress) |
| GET | `/api/sessions/{id}/extraction-status` | status + counts |
| GET | `/api/sessions/{id}/extracted[/claims]` | extracted content |
| POST | `/api/sessions/{id}/chat` | send message (SSE token stream) |
| GET | `/api/sessions/{id}/messages` | history (paginated) |
| GET | `/api/sessions/{id}/sections[/{type}]` | generated sections |
| GET | `/api/prompts` · PUT `/api/prompts/{type}` | instruction-prompt admin |
| GET | `/api/health` | health + model check |

Interactive docs at `/docs` (Swagger UI).

---

## Notes & limits
- Instruction prompts are seeded as **placeholders** and are hot-swappable via
  `PUT /api/prompts/{section_type}` (no redeploy).
- Streaming uses **SSE** (no WebSocket) for both extraction progress and chat.
- Extraction runs the six semantic extractions concurrently.
- Not built (per spec): email, PDF export, collaborative editing, rate limiting.
