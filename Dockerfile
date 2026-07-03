FROM python:3.11-slim

WORKDIR /app

# System deps (python-docx is pure-python, but keep build tooling minimal & clean)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite database + uploaded files live under /data (mount a volume for persistence)
RUN mkdir -p /data /data/uploads

ENV DATABASE_PATH=/data/patent_drafter.db
ENV UPLOAD_DIR=/data/uploads
ENV PORT=8080

EXPOSE 8080

# Single worker: SQLite + in-process state require one process on Cloud Run.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
