import asyncio
import gzip
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from gateway.config import Settings
from gateway.engine import retry_after
from gateway.main import create_app
from gateway.storage import Store, today

ADMIN = "admin-" + "a" * 32
KEY = "project-" + "b" * 32
OTHER = "project-" + "c" * 32
SECRETS = {"GATEWAY_ADMIN_KEY": ADMIN, "PROJECT_KEY": KEY, "OTHER_KEY": OTHER,
           "KEY_A": "secret-upstream-a", "KEY_B": "secret-upstream-b"}
AUTH = {"Authorization": "Bearer " + KEY}
BODY = {"model": "text.default", "messages": [{"role": "user", "content": "private prompt"}], "max_tokens": 32}
PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="


def configuration():
    dep = {"provider": "compatible", "base_url": "https://provider.test/v1", "model": "actual-a",
           "key_env": "KEY_A", "account": "account-a", "quota_group": "quota-a",
           "capabilities": ["text", "vision", "json", "image", "image_to_image", "multiple_reference_images"],
           "billing_error_codes": ["BalanceEmpty"], "rejected_image_error_codes": ["RateLimited"],
           "input_per_million": 1, "output_per_million": 2, "image_price": 0.1}
    backup = {**dep, "model": "actual-b", "key_env": "KEY_B", "account": "account-b", "quota_group": "quota-b"}
    return {"mode": "live", "retry_base_seconds": 0, "projects": {
        "first": {"key_env": "PROJECT_KEY", "models": ["text.default", "image.default"], "daily_limit": 100},
        "second": {"key_env": "OTHER_KEY", "models": ["text.default"], "daily_limit": 100}},
        "routes": {"text.default": {"candidates": ["a", "b"], "attempt_timeout": 0.1, "deadline": 0.5},
                   "image.default": {"kind": "image", "candidates": ["a", "b"], "max_attempts": 2,
                                     "attempt_timeout": 0.1, "deadline": 0.5}},
        "deployments": {"a": dep, "b": backup}}


def ok(content="hello"):
    return httpx.Response(200, json={"id": "provider-request", "choices": [
        {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})


def client_for(tmp_path, handler, config=None):
    return TestClient(create_app(Settings.model_validate(config or configuration()), SECRETS,
                                tmp_path, httpx.MockTransport(handler)))


def post(client, body=None, key=None):
    headers = {**AUTH, **({"Idempotency-Key": key} if key else {})}
    return client.post("/v1/chat/completions", headers=headers, json=body or BODY)


def image_post(client, key="image-1", **changes):
    return client.post("/v1/images/generations", headers={**AUTH, "Idempotency-Key": key},
                       json={"model": "image.default", "prompt": "product", **changes})


def status(client):
    return client.get("/admin/status", headers={"Authorization": "Bearer " + ADMIN}).json()


def test_auth_separation_and_model_access(tmp_path):
    with client_for(tmp_path, lambda req: ok()) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/v1/chat/completions", json=BODY).status_code == 401
        assert client.get("/admin/status", headers=AUTH).status_code == 403
        assert client.get("/v1/models", headers={"Authorization": "Bearer " + ADMIN}).status_code == 401
        assert post(client, {**BODY, "model": "actual-a"}).status_code == 403
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200


def test_gzip_encoded_upstream_response_is_not_decoded_twice(tmp_path):
    def handler(req):
        body = json.dumps({"id": "provider-request", "choices": [
            {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}).encode()
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=gzip.compress(body))
    with client_for(tmp_path, handler) as client:
        response = post(client)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello"


def test_billing_fallback_persists_and_does_not_leak(tmp_path):
    calls = []
    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append(model)
        if model == "actual-a":
            return httpx.Response(429, json={"error": {"code": "BalanceEmpty", "message": SECRETS["KEY_A"]}})
        return ok()
    with client_for(tmp_path, handler) as client:
        response = post(client)
        assert response.status_code == 200
        assert response.headers["x-fallback-used"] == "true"
        assert response.json()["model"] == "actual-b"
        snapshot = status(client)
        assert snapshot["daily"][0]["calls"] == 2
        assert snapshot["blocks"][0]["scope"] == "account:account-a"
        assert SECRETS["KEY_A"] not in json.dumps(snapshot)
        assert "private prompt" not in json.dumps(snapshot)
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    with client_for(tmp_path, handler) as client:
        assert post(client).status_code == 200
    assert calls == ["actual-a", "actual-b", "actual-b"]


def test_same_account_key_cannot_bypass_balance(tmp_path):
    config = configuration()
    config["deployments"]["b"]["account"] = "account-a"
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(402, json={"error": {"code": "BalanceEmpty"}})
    with client_for(tmp_path, handler, config) as client:
        response = post(client)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPSTREAM_BILLING_BLOCKED"
    assert len(calls) == 1


def test_invalid_key_only_blocks_key(tmp_path):
    config = configuration()
    config["deployments"]["b"]["account"] = "account-a"
    def handler(req):
        return httpx.Response(401) if req.headers["authorization"].endswith("-a") else ok()
    with client_for(tmp_path, handler, config) as client:
        assert post(client).status_code == 200
        blocks = status(client)["blocks"]
        assert any(b["scope"] == "key:KEY_A" for b in blocks)
        assert not any(b["scope"].startswith("account:") for b in blocks)


def test_retry_limit_is_global(tmp_path):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(503)
    with client_for(tmp_path, handler) as client:
        assert post(client).status_code == 503
        assert status(client)["daily"][0]["calls"] == 3
    assert len(calls) == 3


def test_deadline_bounds_slow_upstream(tmp_path):
    config = configuration()
    config["routes"]["text.default"].update(attempt_timeout=0.1, deadline=0.04)
    calls = []
    async def handler(req):
        calls.append(req)
        await asyncio.sleep(1)
        return ok()
    with client_for(tmp_path, handler, config) as client:
        start = time.monotonic()
        assert post(client).status_code == 504
        assert time.monotonic() - start < 0.3
    assert len(calls) == 1


@pytest.mark.parametrize("shared", [True, False])
def test_retry_after_respects_quota_group(tmp_path, shared):
    config = configuration()
    if shared:
        config["deployments"]["b"]["quota_group"] = "quota-a"
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "120"}) if len(calls) == 1 else ok()
    with client_for(tmp_path, handler, config) as client:
        assert post(client).status_code == (429 if shared else 200)
        assert len(calls) == (1 if shared else 2)
        assert status(client)["blocks"][0]["until"] > time.time() + 100


@pytest.mark.parametrize("status_code,code", [(400, "BadInput"), (400, "content_policy_violation"), (403, "content_policy_violation")])
def test_permanent_requests_not_retried(tmp_path, status_code, code):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(status_code, json={"error": {"code": code}})
    with client_for(tmp_path, handler) as client:
        assert post(client).status_code == 400
    assert len(calls) == 1


def test_capability_validation_and_json_repair_count(tmp_path):
    config = configuration()
    config["deployments"]["a"]["capabilities"] = ["text", "image"]
    calls = []
    def handler(req):
        calls.append(json.loads(req.content)["model"])
        return ok("invalid json") if len(calls) == 1 else ok('{"ok":true}')
    with client_for(tmp_path, handler, config) as client:
        response = post(client, {**BODY, "response_format": {"type": "json_object"}})
        assert response.status_code == 200
        assert calls == ["actual-b", "actual-b"]
        assert status(client)["daily"][0]["calls"] == 2


@pytest.mark.parametrize("change", [
    {"stream": True}, {"max_tokens": 9000}, {"temperature": "hot"}, {"response_format": {"type":"json_schema"}},
    {"api_base": "http://private"}, {"messages": []}, {"messages": [{"role": [], "content":"x"}]},
    {"messages": [{"role":"user", "content":[{"type":"image_url", "image_url":{"url":"http://127.0.0.1"}}]}]}
])
def test_bad_requests_fail_without_upstream(tmp_path, change):
    calls = []
    def handler(req):
        calls.append(req)
        return ok()
    with client_for(tmp_path, handler) as client:
        assert post(client, {**BODY, **change}).status_code == 400
    assert not calls


def test_daily_limit_includes_retries_and_is_shared_by_alias(tmp_path):
    config = configuration()
    config["projects"]["first"]["daily_limit"] = 1
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(503)
    with client_for(tmp_path, handler, config) as client:
        response = post(client)
        assert response.json()["error"]["code"] == "PROJECT_DAILY_LIMIT"
        assert post(client).status_code == 429
        assert status(client)["daily"][0]["calls"] == 1
    assert len(calls) == 1


def test_atomic_daily_limit_with_threads(tmp_path):
    store = Store(tmp_path / "budget.db")
    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda n: store.start_attempt(str(n), "project", "deployment", 3), range(30)))
        assert sum(x is not None for x in results) == 3
        assert store.overview()["daily"][0]["calls"] == 3
        assert store.overview()["day"] == today()
    finally:
        store.close()


def test_idempotency_replays_success_and_rejects_changed_body(tmp_path):
    calls = []
    def handler(req):
        calls.append(req)
        return ok()
    with client_for(tmp_path, handler) as client:
        first = post(client, key="same")
        second = post(client, key="same")
        assert first.json() == second.json()
        assert first.headers["x-gateway-request-id"] == second.headers["x-gateway-request-id"]
        assert post(client, {**BODY, "max_tokens": 30}, key="same").status_code == 409
        op = first.headers["x-gateway-request-id"]
        assert client.get("/operations/"+op, headers=AUTH).status_code == 200
        assert client.get("/operations/"+op, headers={"Authorization": "Bearer "+OTHER}).status_code == 404
    assert len(calls) == 1


def test_concurrent_duplicate_only_submits_once(tmp_path):
    calls = []
    async def handler(req):
        calls.append(req)
        await asyncio.sleep(0.05)
        return ok()
    with client_for(tmp_path, handler) as client:
        with ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(pool.map(lambda _: post(client, key="concurrent"), range(10)))
        assert all(r.status_code in {200, 202} for r in responses)
    assert len(calls) == 1


def test_image_timeout_unknown_never_retries(tmp_path):
    calls = []
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("upstream key must not leak")
    with client_for(tmp_path, handler) as client:
        first = image_post(client)
        assert first.status_code == 409
        assert first.json()["error"]["code"] == "RESULT_UNKNOWN"
        assert image_post(client).status_code == 409
        assert len(status(client)["unknown"]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, json={"data": []}), httpx.Response(429)])
def test_image_ambiguous_responses_never_retry(tmp_path, response):
    calls = []
    def handler(req):
        calls.append(req)
        return response
    with client_for(tmp_path, handler) as client:
        assert image_post(client).status_code == 409
    assert len(calls) == 1


def test_image_confirmed_rejection_can_fallback(tmp_path):
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(402, json={"error":{"code":"BalanceEmpty"}})
        return httpx.Response(200, json={"data": [{"b64_json": PIXEL}]})
    with client_for(tmp_path, handler) as client:
        first = image_post(client)
        assert first.status_code == 200
        assert image_post(client).json() == first.json()
        assert first.headers["x-fallback-used"] == "true"
        assert client.post("/v1/images/generations", headers=AUTH,
                           json={"model":"image.default","prompt":"x"}).status_code == 400
    assert len(calls) == 2


def test_recovery_marks_inflight_unknown(tmp_path):
    store = Store(tmp_path / "gateway.db")
    op, _, _ = store.claim("first", "crashed", "image.default", "image", {}, "t")
    store.start_attempt(op["id"], "first", "a", 10)
    store.close()
    with client_for(tmp_path, lambda req: ok()) as client:
        response = client.get("/operations/" + op["id"], headers=AUTH)
        assert response.status_code == 409
        assert status(client)["recent"][0]["code"] == "RESULT_UNKNOWN"


def test_admin_reset_and_restart_recovery(tmp_path):
    with client_for(tmp_path, lambda req: httpx.Response(401)) as client:
        post(client)
        scope = status(client)["blocks"][0]["scope"]
        assert client.post("/admin/reset-block", headers=AUTH, json={"scope":scope}).status_code == 403
        response = client.post("/admin/reset-block", headers={"Authorization":"Bearer "+ADMIN}, json={"scope":scope})
        assert response.status_code == 200
        assert scope not in [x["scope"] for x in status(client)["blocks"]]


def test_single_process_lock(tmp_path):
    with client_for(tmp_path, lambda req: ok()):
        with pytest.raises(RuntimeError, match="already in use"):
            with client_for(tmp_path, lambda req: ok()):
                pass


def test_demo_cannot_mix_with_live():
    config = configuration()
    config["deployments"]["a"]["provider"] = "demo"
    with pytest.raises(ValueError, match="cannot be mixed"):
        Settings.model_validate(config)


def test_retry_after_date_and_invalid():
    assert retry_after("60") == 60
    assert retry_after("bad") == 0
    assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0


def test_estimated_cost_tracks_each_attempt(tmp_path):
    with client_for(tmp_path, lambda req: ok()) as client:
        assert post(client).status_code == 200
        record = status(client)["recent"][0]
        assert record["estimated_cost"] == pytest.approx(0.00005)
        assert record["cost_status"] == "estimated"


def test_queue_is_bounded_and_other_project_can_use_its_daily_limit(tmp_path):
    config = configuration()
    config["text_concurrency"] = 1
    config["text_queue_timeout"] = 0.01
    async def handler(req):
        await asyncio.sleep(0.08)
        return ok()
    with client_for(tmp_path, handler, config) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: post(client), range(4)))
        assert any(x.status_code == 429 for x in responses)
        assert sum(x.status_code == 200 for x in responses) == 1


def test_expired_success_keeps_idempotency_tombstone(tmp_path):
    with client_for(tmp_path, lambda req: ok()) as client:
        first = post(client, key="old")
        store = client.app.state.store
        with store.transaction() as db:
            db.execute("UPDATE operations SET updated=? WHERE id=?", (time.time()-8*86400,first.headers["x-gateway-request-id"]))
        store.cleanup()
        assert post(client, key="old").status_code == 410
