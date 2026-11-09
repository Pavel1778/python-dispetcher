"""Voice notes: store a recording, and transcribe it if a provider exists.

Recording happens in the browser (``MediaRecorder``); the server stores the
audio bytes in PostgreSQL and, when a transcription provider is configured,
stores the transcript too. Two facts are kept strictly separate, because
conflating them is exactly the kind of dishonesty this project avoids:

* ``transcript`` + ``transcript_engine`` describe where the text came from. A
  transcript supplied by the browser's own speech recognition is recorded with
  ``engine="browser"`` and is **not** treated as server-verified.
* A server-side engine (``engine="server"``) would be recorded only when the
  backend actually ran a recognizer. No recognizer ships in this repository, so
  with no configuration the transcript is ``None`` and the API says so rather
  than inventing text.

The bytes are bounded by the same upload limit as any other file, so a huge
recording cannot exhaust the database.
"""

from __future__ import annotations

from typing import Any

from .cloud_store import NotFound, PostgresStore
from .file_lab import max_upload_bytes
from .nexus_projects import _text

#: Content types a browser recorder actually produces. Kept permissive but
#: bounded: anything else is refused rather than stored as an opaque blob.
_ALLOWED_AUDIO_PREFIXES = ("audio/", "video/webm", "application/ogg")


class VoiceNoteService:
    def __init__(self, store: PostgresStore):
        self.store = store

    @staticmethod
    def _project(cur: Any, principal: str, project_id: str) -> None:
        cur.execute(
            "SELECT 1 FROM nexus_projects p JOIN nexus_project_members m "
            "ON m.project_id=p.id WHERE p.id=%s AND m.principal=%s",
            (project_id, principal),
        )
        if cur.fetchone() is None:
            raise NotFound("project not found")

    def create(self, principal: str, project_id: str, *, content_type: Any,
               data: bytes, duration_ms: Any = None,
               transcript: Any = None, engine: str | None = None) -> dict:
        content_type = _text(content_type, "content_type", maximum=200)
        if not content_type.startswith(_ALLOWED_AUDIO_PREFIXES):
            raise ValueError("content_type must be an audio type")
        if not isinstance(data, bytes) or not data:
            raise ValueError("audio data is required")
        if len(data) > max_upload_bytes():
            raise ValueError("recording exceeds the size limit")
        duration: int | None = None
        if duration_ms is not None:
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
                raise ValueError("duration_ms must be a non-negative integer")
            duration = duration_ms
        text: str | None = None
        if transcript is not None:
            text = _text(transcript, "transcript", maximum=20000, required=False) or None
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "INSERT INTO nexus_voice_notes(project_id, content_type, size, "
                "duration_ms, data, transcript, transcript_engine, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id, project_id, content_type, size, duration_ms, transcript, "
                "transcript_engine, created_at",
                (project_id, content_type, len(data), duration, data, text,
                 engine if text else None, principal),
            )
            row = dict(cur.fetchone())
        row["id"] = str(row["id"])
        return row

    def list_notes(self, principal: str, project_id: str, *, limit: int = 200) -> list[dict]:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "SELECT id, content_type, size, duration_ms, transcript, "
                "transcript_engine, created_at FROM nexus_voice_notes "
                "WHERE project_id=%s ORDER BY created_at DESC, id LIMIT %s",
                (project_id, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["id"] = str(r["id"])
        return rows

    def get_audio(self, principal: str, project_id: str, note_id: str) -> dict:
        with self.store._tx() as cur:
            self._project(cur, principal, project_id)
            cur.execute(
                "SELECT id, content_type, size, data FROM nexus_voice_notes "
                "WHERE id=%s AND project_id=%s",
                (note_id, project_id),
            )
            row = cur.fetchone()
        if row is None:
            raise NotFound("voice note not found")
        row = dict(row)
        row["id"] = str(row["id"])
        return row
