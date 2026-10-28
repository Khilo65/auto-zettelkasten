import asyncio
from dataclasses import replace
from io import BytesIO
import json
import ssl
import urllib.error

import pytest

from auto_zettelkasten.framework_comparison import ComparisonError, StageBound
from auto_zettelkasten.framework_http import SingleAttemptHTTPSBackend, _NoRedirect, _direct_https_opener
from auto_zettelkasten.framework_transport import MeteredCallLedger, MeteredChatTransport, TransportApproval


ENDPOINT = "https://provider.example/v1/chat/completions"


def approval():
    bound = StageBound("graphiti", "query", 2, 32768, 4096, "0.0000001", "0.0000005",
                       "https://example.org/synthetic-pricing", "synthetic-pinned")
    return TransportApproval("fixture-only", "a" * 64, "b" * 64, ENDPOINT, "0.02",
                             (("graphiti", "0.02"),), (bound,))


def payload():
    return {"model": "synthetic-pinned", "messages": [{"role": "user", "content": "Synthetic evidence"}],
            "max_completion_tokens": 100, "service_tier": "default", "store": False,
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"}}


def completion():
    return {"model": "synthetic-pinned", "service_tier": "default",
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cache_write_tokens": 0}},
            "choices": [{"message": {"role": "assistant", "content": "result"}}]}


class Reply(BytesIO):
    def __init__(self, body=None, *, status=200, url=ENDPOINT, headers=None):
        super().__init__(json.dumps(completion()).encode() if body is None else body)
        self.status, self.url = status, url
        self.headers = {"Content-Type": "application/json", **(headers or {})}
    def geturl(self):
        return self.url
    def getcode(self):
        return self.status


class Opener:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []
    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.reply if self.reply is not None else Reply()


def test_no_default_credential_or_approval_can_open_network():
    opener = Opener()
    backend = SingleAttemptHTTPSBackend(opener=opener)
    with pytest.raises(ComparisonError, match="explicit credential"):
        asyncio.run(backend(endpoint=ENDPOINT, payload=payload()))
    assert not opener.calls


def test_single_attempt_serializes_all_metered_fields_without_typed_sdk_loss(tmp_path):
    opener = Opener()
    backend = SingleAttemptHTTPSBackend(approval=approval(), api_key="fixture-secret-never-real", opener=opener)
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    transport = MeteredChatTransport(ledger, backend=backend)
    result = asyncio.run(transport(framework="graphiti", stage="query", request_id="one", payload=payload()))
    assert result["metering"]["status"] == "settled"
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.full_url == ENDPOINT and request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer fixture-secret-never-real"
    assert json.loads(request.data) == payload()
    assert timeout == 30
    assert "fixture-secret" not in repr(backend)


def test_backend_receipt_must_match_ledger_before_reservation(tmp_path):
    policy = approval()
    opener = Opener()
    backend = SingleAttemptHTTPSBackend(approval=replace(policy, context_sha256="c" * 64), api_key="fixture", opener=opener)
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", policy)
    transport = MeteredChatTransport(ledger, backend=backend)
    with pytest.raises(ComparisonError, match="backend approval differs"):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="one", payload=payload()))
    assert not opener.calls and not ledger.snapshot()["calls"]
    backend._approval = policy
    with pytest.raises(ComparisonError, match="receipt was rebound"):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="rebound", payload=payload()))
    assert not ledger.snapshot()["calls"]
    # The original construction receipt remains different, so its own wire
    # validation rejects the rebind; it can never send under a new receipt.
    with pytest.raises(ComparisonError, match="differs from the installed receipt"):
        asyncio.run(backend(endpoint=ENDPOINT, payload=payload()))
    assert not opener.calls


@pytest.mark.parametrize("endpoint", ["http://provider.example/v1/chat/completions", "https://other.example/v1/chat/completions", ENDPOINT + "?override=1"])
def test_endpoint_override_is_rejected_before_open(endpoint):
    opener = Opener()
    backend = SingleAttemptHTTPSBackend(approval=approval(), api_key="fixture", opener=opener)
    with pytest.raises(ComparisonError, match="endpoint"):
        asyncio.run(backend(endpoint=endpoint, payload=payload()))
    assert not opener.calls


@pytest.mark.parametrize("field,value", [("service_tier", "priority"), ("store", True), ("prompt_cache_options", {"mode": "implicit"})])
def test_pricing_or_storage_fields_cannot_change_before_wire(field, value):
    opener = Opener()
    backend = SingleAttemptHTTPSBackend(approval=approval(), api_key="fixture", opener=opener)
    changed = payload()
    changed[field] = value
    with pytest.raises(ComparisonError, match="wire fields"):
        asyncio.run(backend(endpoint=ENDPOINT, payload=changed))
    assert not opener.calls


@pytest.mark.parametrize("reply", [
    Reply(status=302), Reply(url="https://other.example/"),
    Reply(headers={"Content-Type": "text/html"}), Reply(headers={"Content-Length": "999999999"}),
    Reply(headers={"Content-Length": "invalid"}), Reply(body=b"x" * 101),
    Reply(body=b"not-json"), Reply(body=b'[{"unexpected":true}]'), Reply(body=b'{"error":"private response"}'),
])
def test_bounded_response_failures_do_not_retry(reply):
    opener = Opener(reply=reply)
    backend = SingleAttemptHTTPSBackend(approval=approval(), api_key="fixture", opener=opener, max_response_bytes=100)
    with pytest.raises(ComparisonError):
        asyncio.run(backend(endpoint=ENDPOINT, payload=payload()))
    assert len(opener.calls) == 1


def test_http_error_is_sanitized_and_reservation_retained(tmp_path):
    error = urllib.error.HTTPError(ENDPOINT, 429, "fixture-secret private-note", {}, None)
    opener = Opener(error=error)
    backend = SingleAttemptHTTPSBackend(approval=approval(), api_key="fixture-secret", opener=opener)
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    transport = MeteredChatTransport(ledger, backend=backend)
    with pytest.raises(ComparisonError) as failed:
        asyncio.run(transport(framework="graphiti", stage="query", request_id="one", payload=payload()))
    assert "fixture-secret" not in str(failed.value) and "private-note" not in str(failed.value)
    assert "HTTP 429" in str(failed.value)
    assert len(opener.calls) == 1
    assert ledger.snapshot()["calls"][0]["status"] == "unknown"
    with pytest.raises(ComparisonError, match="no automatic retry"):
        asyncio.run(transport(framework="graphiti", stage="query", request_id="two", payload=payload()))
    assert len(opener.calls) == 1


def test_backend_contract_reason_survives_meter_without_raw_provider_text(tmp_path):
    opener = Opener(reply=Reply(headers={"Content-Length": "999999999"}))
    backend = SingleAttemptHTTPSBackend(approval=approval(), api_key="fixture-secret", opener=opener)
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    with pytest.raises(ComparisonError, match="response exceeds the approved read limit; reservation retained"):
        asyncio.run(MeteredChatTransport(ledger, backend=backend)(framework="graphiti", stage="query", request_id="one", payload=payload()))
    assert ledger.snapshot()["calls"][0]["status"] == "unknown"
    assert len(opener.calls) == 1


def test_http_machine_error_ids_survive_without_message_body_or_headers(tmp_path):
    body = json.dumps({"error": {"message": "SECRET-KEY private prompt and https://private.example/",
                               "type": "invalid_request_error", "code": "unsupported_parameter",
                               "param": "prompt_cache_options"}}).encode()
    error = urllib.error.HTTPError(ENDPOINT, 400, "SECRET-KEY reason", {"Secret": "SECRET-KEY"}, BytesIO(body))
    opener = Opener(error=error)
    backend = SingleAttemptHTTPSBackend(approval=approval(), api_key="fixture-secret", opener=opener)
    ledger = MeteredCallLedger(tmp_path / "meter.sqlite3", approval())
    with pytest.raises(ComparisonError) as failed:
        asyncio.run(MeteredChatTransport(ledger, backend=backend)(framework="graphiti", stage="query", request_id="one", payload=payload()))
    message = str(failed.value)
    assert "HTTP 400" in message and "code=unsupported_parameter" in message
    assert "param=prompt_cache_options" in message and "type=invalid_request_error" in message
    assert "SECRET" not in message and "private" not in message and "https:" not in message
    assert ledger.snapshot()["calls"][0]["status"] == "unknown" and len(opener.calls) == 1


@pytest.mark.parametrize("body", [
    b'{"error":{"message":"SECRET","code":"sk-secret","type":"invalid_request_error SECRET","param":"messages.secret_key"}}',
    b"<html>SECRET provider content</html>",
    b"x" * (16 * 1024 + 1),
])
def test_untrusted_or_oversized_error_body_does_not_escape(body):
    error = urllib.error.HTTPError(ENDPOINT, 400, "SECRET reason", {}, BytesIO(body))
    opener = Opener(error=error)
    backend = SingleAttemptHTTPSBackend(approval=approval(), api_key="fixture", opener=opener)
    with pytest.raises(ComparisonError) as failed:
        asyncio.run(backend(endpoint=ENDPOINT, payload=payload()))
    message = str(failed.value)
    assert "HTTP 400" in message and "SECRET" not in message and "messages.secret_key" not in message
    assert "[" not in message and len(opener.calls) == 1


def test_redirect_handler_never_constructs_redirect_request():
    with pytest.raises(ComparisonError, match="redirects are disabled"):
        _NoRedirect().redirect_request(None, None, 307, "temporary", {}, "https://other.example/")


def test_default_opener_disables_proxies_and_validates_tls(monkeypatch):
    import auto_zettelkasten.framework_http as module
    captured = []
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *handlers: captured.extend(handlers))
    _direct_https_opener()
    proxy = next(h for h in captured if isinstance(h, module.urllib.request.ProxyHandler))
    tls = next(h for h in captured if isinstance(h, module.urllib.request.HTTPSHandler))
    assert proxy.proxies == {}
    assert tls._context.check_hostname is True
    assert tls._context.verify_mode == ssl.CERT_REQUIRED
    assert any(isinstance(h, _NoRedirect) for h in captured)


@pytest.mark.parametrize("kwargs", [{"timeout_seconds": 0}, {"timeout_seconds": 121}, {"timeout_seconds": float("nan")},
                                     {"max_response_bytes": 2000001}, {"api_key": "newline\ninvalid"}])
def test_unbounded_or_header_injection_configuration_rejected(kwargs):
    with pytest.raises(ComparisonError):
        SingleAttemptHTTPSBackend(**kwargs)
