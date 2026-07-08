FROM python:3.11-slim

WORKDIR /app

# System deps (python-docx is pure-python, but keep build tooling minimal & clean)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# All persistence is in Turso (remote libSQL). Only ephemeral upload copies are
# written to local disk (source of truth is the parsed text stored in Turso).
ENV UPLOAD_DIR=/tmp/uploads
ENV PORT=8080

EXPOSE 8080

# Single worker keeps in-process SSE/stream state on one process on Cloud Run.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
