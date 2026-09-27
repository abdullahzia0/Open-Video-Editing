"""Transactional local metadata and job queue. One worker owns rendering at a time."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ove.domain.errors import OveError
from ove.utilities.identity import canonical, new_id, now

#: Object kinds whose records may be enumerated. Prevents an accidental full scan
#: of unrelated tables and keeps the JSON path expression bounded to known fields.
LISTABLE_KINDS = {"asset", "project", "revision", "plan", "artifact", "transcript"}
SEARCHABLE_FIELDS = {"job_id", "asset_id", "project_id", "kind"}


class SQLiteRepository:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(kind,id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT, error TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            if db.execute("SELECT version FROM schema_version").fetchone()[0] != 1:
                raise OveError("schema_mismatch", "Unsupported database schema version.")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind: str, object_id: str, body: dict[str, Any]) -> None:
        with self.connection() as db:
            try:
                db.execute("INSERT INTO objects VALUES (?,?,?)", (kind, object_id, canonical(body)))
            except sqlite3.IntegrityError as exc:
                raise OveError("conflict", "Immutable object already exists.") from exc

    def get(self, kind: str, object_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT body FROM objects WHERE kind=? AND id=?", (kind, object_id)
            ).fetchone()
        if row is None:
            raise OveError("not_found", f"{kind} does not exist.")
        result: dict[str, Any] = json.loads(row["body"])
        return result

    def list_objects(self, kind: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if kind not in LISTABLE_KINDS:
            raise OveError("invalid_request", "This record kind cannot be listed.")
        with self.connection() as db:
            rows = db.execute(
                "SELECT body FROM objects WHERE kind=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (kind, limit, offset),
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def find_objects(
        self, kind: str, field: str, value: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        if kind not in LISTABLE_KINDS or field not in SEARCHABLE_FIELDS:
            raise OveError("invalid_request", "This record query is not supported.")
        with self.connection() as db:
            rows = db.execute(
                "SELECT body FROM objects WHERE kind=? AND json_extract(body, '$.' || ?) = ? "
                "ORDER BY rowid LIMIT ?",
                (kind, field, value, limit),
            ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def revise_project(
        self, project_id: str, expected: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM objects WHERE kind='project' AND id=?", (project_id,)
            ).fetchone()
            if row is None:
                raise OveError("not_found", "Project does not exist.")
            project: dict[str, Any] = json.loads(row["body"])
            if project["revision"] != expected:
                raise OveError("revision_conflict", "Project changed; fetch the latest revision.")
            project = {**project, **changes, "revision": expected + 1}
            db.execute(
                "INSERT INTO objects VALUES ('revision',?,?)",
                (f"{project_id}:{expected + 1}", canonical(project)),
            )
            db.execute(
                "UPDATE objects SET body=? WHERE kind='project' AND id=?",
                (canonical(project), project_id),
            )
        return project

    def submit(
        self, kind: str, payload: dict[str, Any], key: str, fingerprint: str
    ) -> dict[str, Any]:
        if not key or len(key) > 200:
            raise OveError("invalid_idempotency_key", "Use an idempotency key of 1-200 characters.")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint or existing["kind"] != kind:
                    raise OveError(
                        "idempotency_conflict",
                        "This idempotency key was already used for different work.",
                        "Reuse the original arguments, or pass a new idempotency_key.",
                    )
                return self._job(existing)
            job_id = new_id("job")
            timestamp = now()
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,'queued',NULL,NULL,?,?,?,?)",
                (job_id, kind, canonical(payload), key, fingerprint, timestamp, timestamp),
            )
        return self.job(job_id)

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("payload", "result", "error"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def job(self, job_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise OveError("not_found", "Job does not exist.")
        return self._job(row)

    def claim(self) -> dict[str, Any] | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET state='running', updated_at=? WHERE id=?", (now(), row["id"])
            )
        return self.job(row["id"])

    def transition(
        self,
        job_id: str,
        state: str,
        result: dict[str, Any] | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise OveError("not_found", "Job does not exist.")
            current = row["state"]
            if current == state == "cancelled":
                return
            if current == "cancel_requested":
                state, result = "cancelled", None
            allowed = {
                "running": {"validating", "succeeded", "failed", "cancelled"},
                "validating": {"succeeded", "validation_failed", "failed", "cancelled"},
                "cancel_requested": {"cancelled"},
            }
            if state not in allowed.get(current, set()):
                raise OveError("invalid_transition", f"Cannot change {current} to {state}.")
            db.execute(
                "UPDATE jobs SET state=?,result=?,error=?,updated_at=? WHERE id=?",
                (
                    state,
                    canonical(result) if result else None,
                    canonical(error) if error else None,
                    now(),
                    job_id,
                ),
            )

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' "
                "ELSE 'cancel_requested' END, updated_at=? "
                "WHERE id=? AND state IN ('queued','running','validating')",
                (now(), job_id),
            )
        return self.job(job_id)

    def recover(self) -> int:
        # Called only with the exclusive worker process lock held.
        with self.connection() as db:
            cursor = db.execute(
                "UPDATE jobs SET state=CASE WHEN state='cancel_requested' THEN 'cancelled' "
                "ELSE 'failed' END, error=?,updated_at=? "
                "WHERE state IN ('running','validating','cancel_requested')",
                (
                    canonical(
                        {
                            "code": "worker_interrupted",
                            "message": "Worker stopped before completion.",
                            "action": "Retry with a new idempotency key.",
                            "retryable": True,
                        }
                    ),
                    now(),
                ),
            )
            return cursor.rowcount
