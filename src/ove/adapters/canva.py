"""Canva Connect REST adapter.

Implements the documented OAuth 2.0 authorization-code flow with PKCE, encrypted
per-user credential storage, and the REST operations Canva actually publishes:
user profile, design listing/reading/creation, asset upload and design export.

Two boundaries are stated rather than simulated:

* Headless element-level design editing is **not** a REST capability. Canva
  exposes it through the in-editor Design Editing API and its MCP surface, which
  require an interactive editor context. ``edit_design`` therefore reports
  ``UNSUPPORTED_FORMAT`` with the manual route instead of pretending to edit.
* Uploading an asset is not proof that it was inserted into a design, and an
  export URL is not durable storage. Callers receive only what was verified.

Access tokens, refresh tokens and the client secret never leave this module.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ove.config import Settings
from ove.domain.errors import OveError
from ove.security.secrets import SecretStore

CREDENTIAL_NAME = "canva"
PENDING_NAME = "canva_pending"
PENDING_TTL_SECONDS = 600
REFRESH_MARGIN_SECONDS = 60

#: REST operations this adapter can perform, and the scope each one needs.
SUPPORTED_OPERATIONS = {
    "list_designs": "design:meta:read",
    "get_design": "design:meta:read",
    "create_design": "design:content:write",
    "upload_asset": "asset:write",
    "export_design": "design:content:read",
}

STATUS_MAP = {
    400: ("INVALID_INPUT", "Canva rejected the request."),
    401: ("AUTH_REQUIRED", "Canva rejected the access token."),
    403: ("PERMISSION_DENIED", "The connected Canva account may not perform this operation."),
    404: ("FILE_NOT_FOUND", "The requested Canva object does not exist."),
    429: ("EXTERNAL_SERVICE_ERROR", "Canva rate-limited this connection."),
}


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class CanvaRestAdapter:
    """DesignProvider implementation for the verified Canva Connect REST surface."""

    def __init__(self, settings: Settings, store: SecretStore):
        self.settings = settings
        self.store = store

    # ------------------------------------------------------------------ capability

    def capabilities(self) -> dict[str, Any]:
        configured = bool(self.settings.canva_enabled and self.settings.canva_client_id)
        connected = configured and self._credentials() is not None
        return {
            "provider": "canva",
            "available": configured,
            "connected": connected,
            "transport": "rest",
            "reason": None if configured else "disabled",
            "action": None
            if configured
            else "Register a Canva integration, then set OVE_CANVA_ENABLED, "
            "OVE_CANVA_CLIENT_ID and OVE_CANVA_CLIENT_SECRET.",
            "operations": sorted(SUPPORTED_OPERATIONS) if connected else [],
            "operations_available_when_connected": sorted(SUPPORTED_OPERATIONS),
            "unsupported_operations": {
                "edit_design": "Canva exposes element-level editing only through the in-editor "
                "Design Editing API and its MCP editing transactions, which need an interactive "
                "editor context. This headless REST adapter cannot perform it.",
                "insert_asset_into_design": "Uploading an asset is not the same as placing it. "
                "Use Canva's image-to-design import or place it manually in the editor.",
            },
            "scopes": list(self.settings.canva_scopes),
            "offline_mode": self.settings.offline,
        }

    # ----------------------------------------------------------------- credentials

    def _credentials(self) -> dict[str, Any] | None:
        return self.store.get(CREDENTIAL_NAME)

    def _require_credentials(self) -> dict[str, Any]:
        if not self.settings.canva_enabled:
            raise OveError(
                "provider_unavailable",
                "The Canva integration is not configured on this server.",
                "Set OVE_CANVA_ENABLED, OVE_CANVA_CLIENT_ID and OVE_CANVA_CLIENT_SECRET.",
            )
        credentials = self._credentials()
        if credentials is None:
            raise OveError(
                "not_connected",
                "No Canva account is connected.",
                "Call canva_connect with action 'start', then 'complete' with the returned code.",
            )
        return credentials

    def _refresh(self, credentials: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": credentials["refresh_token"],
        }
        body = self._token_request(payload)
        merged = {**credentials, **body}
        merged["expires_at"] = time.time() + float(body.get("expires_in", 0))
        self.store.set(CREDENTIAL_NAME, merged)
        return merged

    def _access_token(self) -> str:
        credentials = self._require_credentials()
        if time.time() >= float(credentials.get("expires_at", 0)) - REFRESH_MARGIN_SECONDS:
            if not credentials.get("refresh_token"):
                raise OveError(
                    "not_connected",
                    "The Canva authorization expired and cannot be refreshed.",
                    "Call canva_connect again to re-authorize.",
                )
            credentials = self._refresh(credentials)
        return str(credentials["access_token"])

    # -------------------------------------------------------------------- transport

    def _guard_network(self) -> None:
        if self.settings.offline:
            raise OveError(
                "provider_unavailable",
                "This server runs in offline mode; external providers are blocked.",
                "Set OVE_OFFLINE=false to permit authorized provider calls.",
            )

    def _http(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        accept: str = "application/json",
    ) -> tuple[int, bytes, dict[str, str]]:
        self._guard_network()
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        request.add_header("Accept", accept)
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.provider_timeout_seconds
            ) as response:
                payload = response.read(self.settings.max_provider_response_bytes + 1)
                if len(payload) > self.settings.max_provider_response_bytes:
                    raise OveError(
                        "resource_limit", "The provider response exceeded the configured limit."
                    )
                return response.status, payload, dict(response.headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read(20_000).decode(errors="replace")
            raise self._http_error(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise OveError(
                "provider_error",
                f"The Canva service could not be reached: {exc.reason}",
                "Check network access and try again.",
                retryable=True,
            ) from exc

    @staticmethod
    def _provider_detail(detail: str) -> str:
        try:
            parsed = json.loads(detail)
        except ValueError:
            return detail[:300]
        if isinstance(parsed, dict):
            code = parsed.get("code") or parsed.get("error")
            message = parsed.get("message") or parsed.get("error_description")
            if code or message:
                return f"{code or 'provider_error'}: {message or ''}".strip()
        return detail[:300]

    def _http_error(self, status: int, detail: str) -> OveError:
        code, message = STATUS_MAP.get(
            status, ("EXTERNAL_SERVICE_ERROR", "Canva returned an error.")
        )
        if status >= 500:
            code = "EXTERNAL_SERVICE_ERROR"
        return OveError(
            code,
            f"{message} ({self._provider_detail(detail)})",
            "Inspect the reported Canva error code before retrying.",
            retryable=status >= 500 or status == 429,
        )

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        token: str | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw, _ = self._http(method, url, headers=headers, body=body)
        if status == 401 and token is not None and retry_auth:
            return self._json_request(
                method, url, payload=payload, token=self._access_token(), retry_auth=False
            )
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise OveError("provider_error", "Canva returned a response that is not JSON.") from exc
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def _authorized_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.settings.canva_api_base}{path}"
        return self._json_request(method, url, payload=payload, token=self._access_token())

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        self._guard_network()
        client_id = self.settings.canva_client_id or ""
        client_secret = self.settings.canva_client_secret or ""
        body = urllib.parse.urlencode(payload).encode()
        status, raw, _ = self._http(
            "POST",
            self.settings.canva_token_base,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": _basic_auth(client_id, client_secret),
            },
            body=body,
        )
        if status != 200:
            raise self._http_error(status, raw.decode(errors="replace"))
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise OveError(
                "provider_error", "The Canva token endpoint returned invalid JSON."
            ) from exc
        if not isinstance(parsed, dict) or not parsed.get("access_token"):
            raise OveError("provider_error", "The Canva token endpoint returned no access token.")
        return parsed

    # ---------------------------------------------------------------------- connect

    def connect(
        self, action: str, code: str | None = None, state: str | None = None
    ) -> dict[str, Any]:
        if action == "start":
            return self._connect_start()
        if action == "complete":
            return self._connect_complete(code, state)
        if action == "status":
            return self._connect_status()
        if action == "disconnect":
            removed = self.store.delete(CREDENTIAL_NAME)
            self.store.delete(PENDING_NAME)
            return {"connected": False, "credentials_removed": removed}
        raise OveError(
            "invalid_request",
            f"Unknown connect action: {action}",
            "Use one of start, complete, status or disconnect.",
        )

    def _connect_start(self) -> dict[str, Any]:
        if not self.settings.canva_enabled or not self.settings.canva_client_id:
            raise OveError(
                "provider_unavailable",
                "The Canva integration is not configured on this server.",
                "Register a Canva integration and set OVE_CANVA_CLIENT_ID/SECRET.",
            )
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        self.store.set(
            PENDING_NAME,
            {"verifier": verifier, "state": state, "created_at": time.time()},
        )
        query = urllib.parse.urlencode(
            {
                "code_challenge": challenge,
                "code_challenge_method": "s256",
                "scope": " ".join(self.settings.canva_scopes),
                "response_type": "code",
                "client_id": self.settings.canva_client_id,
                "redirect_uri": self.settings.canva_redirect_uri,
                "state": state,
            }
        )
        return {
            "status": "authorization_required",
            "authorization_url": f"{self.settings.canva_auth_base}?{query}",
            "state": state,
            "scopes": list(self.settings.canva_scopes),
            "expires_in_seconds": PENDING_TTL_SECONDS,
            "next_step": "Open the authorization URL, approve access, then call canva_connect "
            "with action 'complete' and the 'code' parameter from the redirect.",
            "secrets_exposed": False,
        }

    def _connect_complete(self, code: str | None, state: str | None) -> dict[str, Any]:
        if not code:
            raise OveError("invalid_request", "The authorization code is required.")
        pending = self.store.get(PENDING_NAME)
        if pending is None:
            raise OveError(
                "authorization_pending",
                "No authorization attempt is in progress.",
                "Call canva_connect with action 'start' first.",
            )
        if state is not None and state != pending.get("state"):
            raise OveError(
                "invalid_request",
                "The authorization state does not match the pending request.",
                "Restart the authorization with canva_connect action 'start'.",
            )
        if time.time() - float(pending.get("created_at", 0)) > PENDING_TTL_SECONDS:
            self.store.delete(PENDING_NAME)
            raise OveError(
                "authorization_pending",
                "The authorization request expired.",
                "Call canva_connect with action 'start' again.",
            )
        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": str(pending["verifier"]),
                "redirect_uri": self.settings.canva_redirect_uri,
            }
        )
        credentials = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": time.time() + float(tokens.get("expires_in", 0)),
            "scopes": str(tokens.get("scope", " ".join(self.settings.canva_scopes))).split(),
        }
        self.store.set(CREDENTIAL_NAME, credentials)
        self.store.delete(PENDING_NAME)
        profile = self._profile()
        return {
            "connected": True,
            "account": profile,
            "scopes": credentials["scopes"],
            "expires_at": credentials["expires_at"],
            "secrets_exposed": False,
        }

    def _connect_status(self) -> dict[str, Any]:
        credentials = self._credentials()
        if credentials is None:
            return {"connected": False, "configured": self.settings.canva_enabled}
        return {
            "connected": True,
            "configured": True,
            "account": self._profile(),
            "scopes": credentials.get("scopes", []),
            "expires_at": credentials.get("expires_at"),
            "refreshable": bool(credentials.get("refresh_token")),
            "secrets_exposed": False,
        }

    def _profile(self) -> dict[str, Any]:
        payload = self._authorized_json("GET", "/users/me")
        team = payload.get("team_user", {}) if isinstance(payload, dict) else {}
        return {
            "user_id": team.get("user_id"),
            "team_id": team.get("team_id"),
        }

    # --------------------------------------------------------------------- designs

    def list_designs(
        self, query: str | None, limit: int, continuation: str | None
    ) -> dict[str, Any]:
        params: dict[str, str] = {"limit": str(limit)}
        if query:
            params["query"] = query
        if continuation:
            params["continuation"] = continuation
        payload = self._authorized_json("GET", f"/designs?{urllib.parse.urlencode(params)}")
        return {
            "designs": [self._design_summary(item) for item in payload.get("items", [])],
            "continuation": payload.get("continuation"),
        }

    def get_design(self, design_id: str) -> dict[str, Any]:
        payload = self._authorized_json("GET", f"/designs/{urllib.parse.quote(design_id)}")
        design = payload.get("design", payload)
        return self._design_summary(design)

    @staticmethod
    def _design_summary(design: dict[str, Any]) -> dict[str, Any]:
        urls = design.get("urls", {}) or {}
        thumbnail = design.get("thumbnail", {}) or {}
        return {
            "design_id": design.get("id"),
            "title": design.get("title"),
            "page_count": design.get("page_count"),
            "design_types": design.get("design_types", []),
            "created_at": design.get("created_at"),
            "updated_at": design.get("updated_at"),
            "edit_url": urls.get("edit_url"),
            "view_url": urls.get("view_url"),
            "thumbnail_url": thumbnail.get("url"),
            "urls_are_temporary": True,
        }

    def create_design(self, request: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": request.get("type", "type_and_asset")}
        if request.get("title"):
            payload["title"] = request["title"]
        design_type = request.get("design_type")
        if design_type:
            payload["design_type"] = design_type
        if request.get("asset_id"):
            payload["asset_id"] = request["asset_id"]
        if request.get("design_id"):
            payload["design_id"] = request["design_id"]
        if request.get("brand_template_id"):
            payload["brand_template_id"] = request["brand_template_id"]
        response = self._authorized_json("POST", "/designs", payload)
        design = response.get("design", response)
        result = self._design_summary(design)
        result["note"] = (
            "A blank design created through the API is permanently deleted after 7 days "
            "if it is never edited."
        )
        return result

    def edit_design(self, request: dict[str, Any]) -> dict[str, Any]:
        raise OveError(
            "unsupported_operation",
            "Canva does not publish headless element-level design editing over REST.",
            "Edit in the Canva editor with the Design Editing API, or use the uploaded asset "
            "manually. This adapter will not claim an edit it cannot verify.",
            details={
                "requested": sorted(request),
                "verified_alternative": "https://www.canva.dev/docs/apps/design-editing/",
            },
        )

    # ---------------------------------------------------------------------- assets

    def upload_asset(self, path: Path, name: str) -> dict[str, Any]:
        if not path.is_file():
            raise OveError("not_found", "The asset to upload does not exist.")
        size = path.stat().st_size
        if size == 0:
            raise OveError("invalid_input", "An empty file cannot be uploaded.")
        if size > self.settings.max_provider_response_bytes:
            raise OveError("resource_limit", "The asset exceeds the configured provider limit.")
        metadata = base64.b64encode(json.dumps({"name": name}).encode()).decode()
        self._guard_network()
        status, raw, _ = self._http(
            "POST",
            f"{self.settings.canva_api_base}/asset-uploads",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/octet-stream",
                "Asset-Upload-Metadata": metadata,
            },
            body=path.read_bytes(),
        )
        job = self._decode_json(raw, status)
        job_id = job.get("job", {}).get("id") if isinstance(job.get("job"), dict) else None
        if not job_id:
            raise OveError("provider_error", "Canva accepted the upload but returned no job id.")
        completed = self._poll(
            f"/asset-uploads/{urllib.parse.quote(str(job_id))}", "job", {"success", "failed"}
        )
        state = completed.get("job", {}).get("status")
        if state != "success":
            raise OveError(
                "provider_error",
                f"The Canva asset upload ended in state '{state}'.",
                "Check the file type and size against Canva's current asset limits.",
            )
        asset = completed.get("job", {}).get("asset", {}) or {}
        return {
            "asset_id": asset.get("id"),
            "name": asset.get("name", name),
            "bytes": size,
            "job_id": job_id,
            "verified": True,
            "inserted_into_design": False,
        }

    def _decode_json(self, raw: bytes, status: int) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise OveError("provider_error", "Canva returned a response that is not JSON.") from exc
        if status >= 400:
            raise self._http_error(status, raw.decode(errors="replace"))
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def _poll(self, path: str, wrapper: str, terminal: set[str]) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for _ in range(self.settings.provider_poll_attempts):
            last = self._authorized_json("GET", path)
            block = last.get(wrapper, {}) if isinstance(last.get(wrapper), dict) else {}
            if block.get("status") in terminal:
                return last
            time.sleep(self.settings.provider_poll_seconds)
        raise OveError(
            "provider_error",
            "The Canva operation did not reach a terminal state before the poll budget expired.",
            "Query Canva directly or retry; the remote outcome is unknown.",
            retryable=True,
            details={"last_state": last},
        )

    # --------------------------------------------------------------------- exports

    def export_design(
        self, design_id: str, format_type: str, destination: Path, quality: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "design_id": design_id,
            "format": {"type": format_type},
        }
        if quality and format_type in {"jpg", "png"}:
            payload["format"]["quality"] = quality
        started = self._authorized_json("POST", "/exports", payload)
        export_id = (
            started.get("job", {}).get("id") if isinstance(started.get("job"), dict) else None
        )
        if not export_id:
            raise OveError("export_failed", "Canva accepted the export but returned no job id.")
        completed = self._poll(
            f"/exports/{urllib.parse.quote(str(export_id))}", "job", {"success", "failed"}
        )
        job = completed.get("job", {})
        if job.get("status") != "success":
            raise OveError(
                "export_failed",
                f"The Canva export ended in state '{job.get('status')}'.",
                "Confirm the design supports the requested export format.",
            )
        urls = job.get("urls") or []
        if not urls:
            raise OveError(
                "export_failed", "The Canva export reported success without any file URL."
            )
        destination.mkdir(parents=True, exist_ok=True)
        files = []
        for index, url in enumerate(urls, start=1):
            target = destination / f"canva-export-{index}.{format_type}"
            status, raw, headers = self._http("GET", str(url), accept="application/octet-stream")
            if status != 200 or not raw:
                raise OveError(
                    "export_failed",
                    "The exported file could not be downloaded.",
                    "Re-run the export; Canva export URLs expire quickly.",
                    retryable=True,
                )
            target.write_bytes(raw)
            files.append(
                {
                    "path": str(target),
                    "bytes": len(raw),
                    "content_type": headers.get("Content-Type", "application/octet-stream"),
                }
            )
        return {
            "export_id": export_id,
            "format": format_type,
            "files": files,
            "note": "Canva export URLs are temporary; files were copied into local storage.",
        }

    def design_export_formats(self, design_id: str) -> dict[str, Any]:
        return self._authorized_json(
            "GET", f"/designs/{urllib.parse.quote(design_id)}/export-formats"
        )

    # ---------------------------------------------------------------- generic entry

    def execute(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Operation-specific dispatch. Unknown actions are rejected, never faked."""
        handlers = {
            "connect": lambda: self.connect(
                str(arguments.get("action", "status")),
                arguments.get("code"),
                arguments.get("state"),
            ),
            "list_designs": lambda: self.list_designs(
                arguments.get("query"),
                int(arguments.get("limit", 20)),
                arguments.get("continuation"),
            ),
            "get_design": lambda: self.get_design(str(arguments["design_id"])),
            "create_design": lambda: self.create_design(arguments),
            "upload_asset": lambda: self.upload_asset(
                Path(str(arguments["path"])), str(arguments.get("name", "asset"))
            ),
            "export_design": lambda: self.export_design(
                str(arguments["design_id"]),
                str(arguments.get("format", "png")),
                Path(str(arguments["destination"])),
                arguments.get("quality"),
            ),
            "edit_design": lambda: self.edit_design(arguments),
        }
        if action not in handlers:
            raise OveError(
                "unsupported_operation",
                f"Unknown Canva operation: {action}",
                f"Supported operations: {sorted(handlers)}",
            )
        return handlers[action]()
