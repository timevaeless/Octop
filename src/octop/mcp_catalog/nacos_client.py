"""Minimal nacos v3 admin API client for config get/publish.

Verified against nacos 3.x (summer-harness middleware/ compat layer):
* auth: POST ``/nacos/v1/auth/login`` (form) → ``accessToken``;
* read:  GET  ``/nacos/v3/admin/cs/config`` (query params) → ``data.content``;
* write: POST ``/nacos/v3/admin/cs/config`` — **form-encoded**, not JSON body
  (a JSON body is silently rejected); content type passed via ``type`` field;
* delete: DELETE ``/nacos/v3/admin/cs/config`` (query params) — verified
  against the compat layer (2026-09-25): code=0 on success; a missing config
  is **HTTP 404** for GET (body code 20004) and HTTP 200 + code 0 for DELETE
  (idempotent) — both treated as "not exist".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_TIMEOUT_SECONDS = 10


class NacosConfigError(RuntimeError):
    pass


class NacosConfigClient:
    def __init__(
        self,
        server_addr: str,  # e.g. "http://localhost:8848"
        username: str,
        password: str,
        namespace_id: str = "higress-system",
        group_name: str = "higress-system",
    ) -> None:
        self._base = server_addr.rstrip("/")
        self._username = username
        self._password = password
        self._namespace = namespace_id
        self._group = group_name
        self._token: str | None = None

    # ---- low level ----

    def _login(self) -> str:
        body = urllib.parse.urlencode(
            {"username": self._username, "password": self._password}
        ).encode()
        req = urllib.request.Request(f"{self._base}/nacos/v3/auth/user/login", data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                payload = json.load(resp)
        except (OSError, ValueError) as e:
            raise NacosConfigError(f"nacos login failed: {e}") from e
        token = payload.get("accessToken")
        if not token:
            raise NacosConfigError("nacos login returned no accessToken")
        return str(token)

    def _ensure_token(self) -> str:
        if self._token is None:
            self._token = self._login()
        return self._token

    def _request(self, req: urllib.request.Request) -> dict[str, Any]:
        token = self._ensure_token()
        req.add_header("accessToken", token)
        try:
            return self._send(req)
        except NacosConfigError as e:
            # one retry on a transient network failure (review 3.12): a single
            # blip should not abort a multi-dataId publish midway
            if isinstance(e.__cause__, OSError) and not isinstance(
                e.__cause__, urllib.error.HTTPError
            ):
                return self._send(req)
            raise

    def _send(self, req: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            # review round-7 §4 (same defect as hub NacosAclWatcher): nacos 3.x
            # answers an expired/invalid token with 403 (verified live), not
            # 401 — a 401-only re-login never fires. Both codes mean
            # "credential failure" → re-login once and retry. (Low blast
            # radius here: a client is created per publish request, so tokens
            # rarely live long enough to expire — but the same trap.)
            if e.code in (401, 403):  # token expired/invalid once → re-login and retry
                self._token = None
                req.remove_header("accessToken")
                req.add_header("accessToken", self._ensure_token())
                try:
                    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                        payload = json.load(resp)
                except (OSError, ValueError) as e2:
                    raise NacosConfigError(f"nacos request failed after re-login: {e2}") from e2
            else:
                raise NacosConfigError(f"nacos HTTP {e.code}") from e
        except (OSError, ValueError) as e:
            raise NacosConfigError(f"nacos request failed: {e}") from e
        if not isinstance(payload, dict):
            raise NacosConfigError("nacos returned a non-object payload")
        if payload.get("code", 0) != 0:
            raise NacosConfigError(f"nacos error: {payload}")
        return payload

    # ---- config API ----

    def admin_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Generic authenticated GET on the v3 admin API (naming queries etc.).

        Reuses the login/retry machinery; ``path`` starts with ``/nacos/...``.
        """
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self._base}{path}?{query}")
        return self._request(req)

    def get_config(self, data_id: str) -> str | None:
        """Return config content, or ``None`` when the config does not exist."""
        query = urllib.parse.urlencode(
            {"namespaceId": self._namespace, "dataId": data_id, "groupName": self._group}
        )
        req = urllib.request.Request(f"{self._base}/nacos/v3/admin/cs/config?{query}")
        try:
            payload = self._request(req)
        except NacosConfigError as e:
            # Verified live (2026-09-25, round-2 review M1): a missing config
            # returns HTTP 404 with body {"code":20004,...}; urllib surfaces
            # that as HTTPError before any body code is visible, so the only
            # reliable signal is the HTTP status carried in the message.
            if "http 404" in str(e).lower():
                return None
            raise
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("content") is None:
            return None
        return str(data["content"])

    def publish_config(self, data_id: str, content: str, *, content_type: str = "json") -> None:
        body = urllib.parse.urlencode(
            {
                "namespaceId": self._namespace,
                "dataId": data_id,
                "groupName": self._group,
                "content": content,
                "type": content_type,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self._base}/nacos/v3/admin/cs/config",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        self._request(req)

    def delete_config(self, data_id: str) -> bool:
        """Delete a config. Returns ``True`` when deleted, ``False`` when it
        did not exist (idempotent — safe to call on every unpublish)."""
        query = urllib.parse.urlencode(
            {"namespaceId": self._namespace, "dataId": data_id, "groupName": self._group}
        )
        req = urllib.request.Request(
            f"{self._base}/nacos/v3/admin/cs/config?{query}", method="DELETE"
        )
        try:
            self._request(req)
            return True
        except NacosConfigError as e:
            # DELETE on a missing config returns HTTP 200 + code 0 (verified
            # live, 2026-09-25), so this branch should never fire; kept as a
            # defensive no-op for a 404, same rationale as get_config.
            if "http 404" in str(e).lower():
                return False
            raise
