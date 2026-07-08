"""
Central configuration. All values are read from environment variables.

Storage is managed entirely by the Turso cloud database (libSQL) — there is no
local SQLite file. TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are required in every
environment that talks to the database.
"""
import os
from pathlib import Path

# --- Anthropic ---------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Model used for every AI operation (extraction + drafting).
# NOTE: This must be a currently-valid Anthropic model id. The spec requested
# "claude-sonnet-4-6"; override via the ANTHROPIC_MODEL env var if that id is not
# available to your account (e.g. "claude-sonnet-5").
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Generous ceilings; extraction + drafting both fit comfortably.
MAX_TOKENS_EXTRACTION = int(os.getenv("MAX_TOKENS_EXTRACTION", "8000"))
MAX_TOKENS_DRAFTING = int(os.getenv("MAX_TOKENS_DRAFTING", "8000"))

# --- Storage (Turso / libSQL cloud database) ---------------------------------
# All persistence lives in Turso. No local database file is ever created.
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

# Uploaded .docx originals are written here for traceability only (the parsed
# raw_text stored in Turso is the source of truth, so this dir is disposable —
# ephemeral container storage on Cloud Run is fine).
UPLOAD_DIR = os.getenv("UPLOAD_DIR", str(Path(__file__).parent / "data" / "uploads"))

# --- Auth --------------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET", "dev-insecure-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = int(os.getenv("JWT_EXPIRY_HOURS", "168"))  # 7 days

# --- Server ------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8080"))

# --- Upload / extraction limits ----------------------------------------------
MAX_DOCS_PER_SESSION = 2
ALLOWED_EXTENSIONS = {".docx"}
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(25 * 1024 * 1024)))  # 25 MB
CHAT_HISTORY_LIMIT = 50  # messages loaded into context per turn

# Retry policy for Anthropic API calls
API_MAX_RETRIES = 1  # spec: "retry once with exponential backoff"
API_RETRY_BASE_DELAY = 2.0  # seconds


def ensure_dirs() -> None:
    """Create the upload directory if it does not exist (no DB dir — Turso is remote)."""
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
