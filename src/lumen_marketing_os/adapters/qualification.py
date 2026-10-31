"""Bounded, injectable HTTP adapters for Department 2 providers."""
from __future__ import annotations

import json
import re
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str]


class ProviderError(Exception):
    def __init__(self, error_class: str, *, retryable: bool = False,
                 status: int | None = None, retry_after_seconds: int | None = None) -> None:
        super().__init__(error_class)
        self.error_class = error_class
        self.retryable = retryable
        self.status = status
        self.retry_after_seconds = retry_after_seconds


Transport = Callable[[str, str, dict[str, str], bytes | None, int], ProviderResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def default_transport(method: str, url: str, headers: dict[str, str],
                      body: bytes | None, timeout: int) -> ProviderResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ProviderError("network_failure", retryable=True) from None
    try:
        if response.status in {301, 302, 303, 307, 308}:
            raise ProviderError("redirect_denied", status=response.status)
        data = response.read(4_000_001)
        if len(data) > 4_000_000:
            raise ProviderError("response_too_large", status=response.status)
        try:
            parsed = json.loads(data) if data else {}
        except (UnicodeDecodeError, ValueError):
            raise ProviderError("invalid_json", status=response.status) from None
        if not isinstance(parsed, dict):
            raise ProviderError("invalid_json_shape", status=response.status)
        return ProviderResponse(response.status, parsed,
                                {key.lower(): value for key, value in response.headers.items()})
    finally:
        response.close()


def _retry_after(headers: dict[str, str]) -> int | None:
    value = headers.get("retry-after", "").strip()
    if value.isdigit():
        return min(86400, max(1, int(value)))
    if value:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return min(86400, max(1, int((deadline - datetime.now(timezone.utc)).total_seconds())))
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def _classify_status(status: int, headers: dict[str, str]) -> ProviderError:
    if status == 429:
        return ProviderError("rate_limited", retryable=True, status=status,
                             retry_after_seconds=_retry_after(headers))
    if status in {401, 403}:
        return ProviderError("access_denied", status=status)
    if status in {408, 425, 500, 502, 503, 504}:
        return ProviderError("provider_unavailable", retryable=True, status=status)
    if status == 404:
        return ProviderError("not_found", status=status)
    return ProviderError("provider_rejected", status=status)


class LumenClient:
    """POST once with an idempotency key, then poll the returned same-origin URL."""

    def __init__(self, base_url: str, api_key: str, *, transport: Transport = default_transport,
                 timeout_seconds: int = 20, allow_http_local: bool = False) -> None:
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        is_local = parsed.hostname in {"localhost", "127.0.0.1"}
        if (parsed.scheme != "https" and not (allow_http_local and is_local and parsed.scheme == "http")):
            raise ValueError("Lumen base URL must be HTTPS (or local HTTP in development)")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid Lumen base URL")
        if not api_key.strip() or timeout_seconds <= 0:
            raise ValueError("Lumen key and positive timeout are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def _call(self, method: str, url: str, body: dict | None = None,
              idempotency_key: str | None = None) -> ProviderResponse:
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        encoded = json.dumps(body, sort_keys=True).encode("utf-8") if body is not None else None
        response = self.transport(method, url, headers, encoded, self.timeout_seconds)
        if response.status not in {200, 202}:
            raise _classify_status(response.status, response.headers)
        return response

    def start(self, payload: dict, request_key: str) -> ProviderResponse:
        if not request_key or not isinstance(payload, dict):
            raise ValueError("request payload and idempotency key required")
        return self._call("POST", self.base_url + "/v1/profiles/lookup", payload, request_key)

    def poll(self, poll_path: str) -> ProviderResponse:
        base = urllib.parse.urlsplit(self.base_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(self.base_url + "/", poll_path))
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise ProviderError("unsafe_poll_url")
        if not re.fullmatch(r"/v1/profiles/[A-Za-z0-9_-]+", target.path) or target.query or target.fragment:
            raise ProviderError("unsafe_poll_url")
        return self._call("GET", urllib.parse.urlunsplit(target))


class HunterClient:
    """Opt-in, bounded Domain Search. Results are candidates, never verified contacts."""

    def __init__(self, api_key: str, *, transport: Transport = default_transport,
                 timeout_seconds: int = 20) -> None:
        if not api_key.strip() or timeout_seconds <= 0:
            raise ValueError("Hunter key and positive timeout are required")
        self.api_key = api_key
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def search(self, domain: str, *, limit: int = 10) -> list[dict]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}", domain) or not 1 <= limit <= 10:
            raise ValueError("valid domain and limit 1..10 required")
        query = urllib.parse.urlencode({"domain": domain, "limit": limit, "type": "personal",
                                        "decision_maker": "true", "api_key": self.api_key})
        response = self.transport("GET", "https://api.hunter.io/v2/domain-search?" + query,
                                  {"Accept": "application/json"}, None, self.timeout_seconds)
        if response.status != 200:
            raise _classify_status(response.status, response.headers)
        data = response.body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("emails"), list):
            raise ProviderError("invalid_provider_response")
        if data.get("domain") not in {domain, None}:
            raise ProviderError("domain_mismatch")
        return [item for item in data["emails"][:limit] if isinstance(item, dict)]
