"""Single-attempt HTTPS wire backend for an already metered experiment call.

No default client is constructed and no credentials are read from the environment.
This backend is an orchestrator-owned dependency of MeteredChatTransport, never a
replacement for its admission ledger or evidence/usage validation.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import math
import ssl
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .framework_comparison import ComparisonError
from .framework_transport import TransportApproval, TransportDiagnosticError, validate_chat_payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportDiagnosticError("redirect")


def _direct_https_opener():
    context = ssl.create_default_context()
    # Explicitly preserve both checks even if this function is refactored later.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), _NoRedirect())


def _http_error_machine_metadata(error: urllib.error.HTTPError) -> dict[str, str]:
    """Read at most 16 KiB, retaining only fixed machine IDs, never messages."""
    limit = 16 * 1024
    try:
        body = error.read(limit + 1)
        if not isinstance(body, bytes) or len(body) > limit:
            return {}
        envelope = json.loads(body.decode("utf-8"))
        if not isinstance(envelope, dict):
            return {}
        return TransportDiagnosticError.sanitize_machine_error(envelope.get("error"))
    except Exception:
        return {}


class SingleAttemptHTTPSBackend:
    """A raw JSON Chat Completions backend, disabled without key and receipt.

    It calls opener.open exactly once. An async timeout stops awaiting the wire
    attempt; it cannot prove the provider cancelled it, so the outer ledger must
    retain its unknown reservation. No failure initiates another request.
    ``opener`` is an explicit test seam; production uses certificate-checked
    direct HTTPS with proxies and redirects disabled.
    """

    def __init__(self, *, approval: TransportApproval | None = None, api_key: str | None = None,
                 timeout_seconds: float = 30.0, max_response_bytes: int = 2_000_000,
                 opener: Any | None = None):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ComparisonError("HTTPS timeout must be finite, positive and at most 120 seconds")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 2_000_000:
            raise ComparisonError("HTTPS response limit must be between 1 and 2000000 bytes")
        if api_key is not None and (not isinstance(api_key, str) or not 1 <= len(api_key) <= 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise ComparisonError("explicit HTTPS credential has an invalid format")
        self._approval = approval
        self._approval_identity = approval.identity() if approval else None
        self._api_key = api_key
        self._timeout = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._opener = opener

    def __repr__(self) -> str:
        return f"SingleAttemptHTTPSBackend(credential_configured={self._api_key is not None}, approval_configured={self._approval is not None})"

    @property
    def approval_identity(self) -> str | None:
        # Recompute as well as retaining the construction identity: an
        # accidental mutable backend rebind must fail before ledger admission.
        current = self._approval.identity() if self._approval else None
        if current != self._approval_identity:
            raise ComparisonError("HTTPS backend receipt was rebound; no reservation or dispatch")
        return current

    def _validate(self, endpoint: str, payload: Mapping[str, Any]) -> bytes:
        if self._approval is None or self._api_key is None:
            raise TransportDiagnosticError("receipt")
        if self._approval.identity() != self._approval_identity or endpoint != self._approval.endpoint:
            raise TransportDiagnosticError("endpoint_receipt")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TransportDiagnosticError("endpoint")
        if not isinstance(payload, Mapping) or payload.get("service_tier") != "default" or payload.get("store") is not False or payload.get("prompt_cache_options") != {"mode": "explicit", "ttl": "30m"}:
            raise TransportDiagnosticError("wire_fields")
        # The outer ledger enforces the particular stage. Revalidate common
        # payload safety without permitting a wire backend to add new fields.
        candidates = [bound for bound in self._approval.stages if bound.model == payload.get("model")]
        for bound in candidates:
            try:
                canonical, _ = validate_chat_payload(payload, bound)
                return json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False).encode("utf-8")
            except ComparisonError:
                continue
        raise TransportDiagnosticError("wire_window")

    async def __call__(self, *, endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        wire = self._validate(endpoint, payload)
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._send, endpoint, wire), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise TransportDiagnosticError("timeout") from None

    def _send(self, endpoint: str, wire: bytes) -> dict[str, Any]:
        request = urllib.request.Request(endpoint, data=wire, method="POST", headers={
            "Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json",
            "Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close",
        })
        opener = self._opener if self._opener is not None else _direct_https_opener()
        try:
            # Exactly one open: urllib's implicit redirect/proxy behavior has
            # been removed. HTTP errors and timeouts are never retried here.
            with opener.open(request, timeout=self._timeout) as response:
                if response.geturl() != endpoint:
                    raise TransportDiagnosticError("response_endpoint")
                status = response.getcode()
                if type(status) is not int or status != 200:
                    if type(status) is int and 100 <= status <= 599:
                        raise TransportDiagnosticError("http_status", http_status=status)
                    raise TransportDiagnosticError("response_status")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise TransportDiagnosticError("response_type")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        length = int(declared)
                    except (TypeError, ValueError):
                        raise TransportDiagnosticError("content_length") from None
                    if not 0 <= length <= self._max_response_bytes:
                        raise TransportDiagnosticError("response_size")
                raw = response.read(self._max_response_bytes + 1)
                if len(raw) > self._max_response_bytes:
                    raise TransportDiagnosticError("response_size")
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict) or "error" in result:
                    raise TransportDiagnosticError("response_envelope")
                return result
        except ComparisonError:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None
            # No arbitrary message, body, reason, header or URL is propagated.
            # Only a bounded JSON object's allowlisted machine IDs survive.
            metadata = _http_error_machine_metadata(exc)
            try:
                exc.close()
            except Exception:
                pass
            raise TransportDiagnosticError("http_status", http_status=status, machine_error=metadata) from None
        except ssl.SSLError:
            raise TransportDiagnosticError("tls") from None
        except TimeoutError:
            raise TransportDiagnosticError("timeout") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TransportDiagnosticError("response_json") from None
        except Exception:
            # No URL, provider body, header, request text or key is included.
            raise TransportDiagnosticError("network") from None
