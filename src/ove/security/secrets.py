"""Encrypted at-rest storage for provider credentials.

Values are written only by adapters and are never returned through a tool result,
a log line or a capability report. The symmetric key lives beside the ciphertext
with owner-only permissions, or is supplied through ``OVE_SECRET_KEY``.
"""

import json
import os
from pathlib import Path
from typing import Any

from ove.domain.errors import OveError


def _import_fernet() -> tuple[Any, Any]:
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise OveError(
            "missing_dependency",
            "Encrypted credential storage is unavailable.",
            "Install the cryptography package to persist provider authorizations.",
        ) from exc
    return Fernet, InvalidToken


class SecretStore:
    """A single-user encrypted key/value store for provider tokens."""

    def __init__(self, path: Path, key: str | None = None):
        self.path = path
        self.key_path = path.with_name(f"{path.name}.key")
        self._material = key
        self._cipher: Any = None

    def _fernet(self) -> Any:
        Fernet, _ = _import_fernet()
        if self._cipher is None:
            material = self._material or self._material_from_file()
            try:
                self._cipher = Fernet(material.encode())
            except (ValueError, TypeError) as exc:
                raise OveError(
                    "invalid_request",
                    "OVE_SECRET_KEY is not a valid Fernet key.",
                    "Generate one with cryptography.fernet.Fernet.generate_key().",
                ) from exc
        return self._cipher

    def _material_from_file(self) -> str:
        Fernet, _ = _import_fernet()
        if self.key_path.is_file():
            return self.key_path.read_text().strip()
        self.key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        material = Fernet.generate_key().decode()
        descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(material)
        return material

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (ValueError, OSError) as exc:
            raise OveError(
                "invalid_request",
                "The credential store is unreadable.",
                "Remove the corrupt store file and authorize the provider again.",
            ) from exc
        if not isinstance(raw, dict):
            raise OveError("invalid_request", "The credential store has an unexpected layout.")
        return {str(key): str(value) for key, value in raw.items()}

    def _write(self, body: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(body, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def set(self, name: str, value: dict[str, Any]) -> None:
        body = self._read()
        body[name] = self._fernet().encrypt(json.dumps(value).encode()).decode()
        self._write(body)

    def get(self, name: str) -> dict[str, Any] | None:
        token = self._read().get(name)
        if token is None:
            return None
        _, InvalidToken = _import_fernet()
        try:
            decoded = json.loads(self._fernet().decrypt(token.encode()))
        except InvalidToken as exc:
            raise OveError(
                "invalid_request",
                "Stored credentials cannot be decrypted with the current key.",
                "Restore the original key file or authorize the provider again.",
            ) from exc
        return decoded if isinstance(decoded, dict) else None

    def delete(self, name: str) -> bool:
        body = self._read()
        if name not in body:
            return False
        del body[name]
        self._write(body)
        return True

    def names(self) -> list[str]:
        return sorted(self._read())
