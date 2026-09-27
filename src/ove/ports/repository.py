"""Persistence contract implemented by the local SQLite adapter."""

from typing import Any, Protocol


class Repository(Protocol):
    def put(self, kind: str, object_id: str, body: dict[str, Any]) -> None: ...

    def get(self, kind: str, object_id: str) -> dict[str, Any]: ...

    def list_objects(self, kind: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]: ...

    def find_objects(
        self, kind: str, field: str, value: str, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    def revise_project(
        self, project_id: str, expected: int, changes: dict[str, Any]
    ) -> dict[str, Any]: ...

    def submit(
        self, kind: str, payload: dict[str, Any], key: str, fingerprint: str
    ) -> dict[str, Any]: ...

    def job(self, job_id: str) -> dict[str, Any]: ...

    def claim(self) -> dict[str, Any] | None: ...

    def transition(
        self,
        job_id: str,
        state: str,
        result: dict[str, Any] | None = None,
        error: dict[str, object] | None = None,
    ) -> None: ...

    def cancel(self, job_id: str) -> dict[str, Any]: ...

    def recover(self) -> int: ...
