"""
Central configuration. All values are read from environment variables with
sensible local-dev defaults so the app runs out of the box and is Cloud Run ready.
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

# --- Storage -----------------------------------------------------------------
DATABASE_PATH = os.getenv("DATABASE_PATH", str(Path(__file__).parent / "data" / "patent_drafter.db"))
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
    """Create data + upload directories if they do not exist."""
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
