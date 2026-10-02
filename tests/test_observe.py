import json
import sqlite3
import time

import httpx
import pytest
from gateway.observe import Observer
from gateway.storage import Store
from test_gateway import ADMIN, AUTH, SECRETS, client_for, ok, post

ADMIN_AUTH = {"Authorization": "Bearer " + ADMIN}


def test_observe_auth_fallback_idempotency_and_redaction(tmp_path):
    def handler(request):
        if json.loads(request.content)["model"] == "actual-a":
            return httpx.Response(402, json={"error": {"code": "BalanceEmpty", "message": "secret text"}})
        return ok("private output")

    with client_for(tmp_path, handler) as client:
        response = post(client, key="dashboard-idem")
        op_id = response.headers["x-gateway-request-id"]
        assert post(client, key="dashboard-idem").status_code == 200
        for path in ("analytics", "requests", f"requests/{op_id}"):
            url = "/admin/observe/" + path
            assert client.get(url).status_code == 401
            assert client.get(url, headers=AUTH).status_code == 403
            r = client.get(url, headers=ADMIN_AUTH)
            assert r.status_code == 200
            for secret in ["private prompt", "private output", "secret text", "dashboard-idem", *SECRETS.values()]:
                assert secret not in r.text
        d = client.get("/admin/observe/analytics", headers=ADMIN_AUTH).json()
        assert d["requests"] == 1 and d["success_rate"] == 100
        assert d["retries"] == 1 and d["usage"]["calls"] == 2
        assert d["usage"]["input_tokens"] == 10 and d["usage"]["output_tokens"] == 20
        assert d["usage"]["not_accepted_calls"] == 1
        assert d["usage"]["unpriced_calls"] == 0
        assert {g["name"] for g in d["groups"]["model"]} == {"actual-a", "actual-b"}
        detail = client.get(f"/admin/observe/requests/{op_id}", headers=ADMIN_AUTH).json()
        assert [a["deployment"] for a in detail["attempts"]] == ["a", "b"]
        assert [a["actual_model"] for a in detail["attempts"]] == ["actual-a", "actual-b"]
        assert client.get("/admin/observe/requests/missing", headers=ADMIN_AUTH).status_code == 404


def seed(store, project="p", state="succeeded", kind="chat", ago=0, elapsed=1, usage=None,
         currency="CNY", cost=.1, attempt=True, code="ok", model="model-a"):
    op, _, _ = store.claim(project, str(time.time_ns()), "text.default", kind, {}, "task-tag")
    if attempt:
        a = store.start_attempt(op["id"], project, "a", 1000, model)
        store.finish_attempt(a, elapsed, code, currency, usage, cost, "estimated" if cost is not None else "unknown")
    store.finish(op["id"], state, result={"secret": "not for dashboard"}, model=model)
    started = time.time() - ago
    with store.transaction() as db:
        db.execute("UPDATE operations SET created=?,updated=? WHERE id=?", (started, started + elapsed, op["id"]))
        db.execute("UPDATE attempts SET started=? WHERE operation_id=?", (started, op["id"]))
    return op["id"]


def test_statistics_missing_usage_currencies_images_and_expired(tmp_path):
    store = Store(tmp_path / "stats.db")
    try:
        seed(store, usage={"prompt_tokens": 10, "completion_tokens": 20}, ago=20, elapsed=2)
        seed(store, state="failed", usage={"prompt_tokens": 5}, currency="USD", cost=.2, ago=10, elapsed=5)
        seed(store, kind="image", usage={}, ago=15)
        seed(store, state="unknown", cost=None, ago=12)
        seed(store, state="running", cost=None, code="running", ago=10)
        seed(store, state="expired", usage={"prompt_tokens": 0, "completion_tokens": 0}, ago=8*86400)
        seed(store, project="outside", ago=31*86400)
        d = Observer(store).analytics(30)
        assert d["requests"] == 6 and d["succeeded"] == 3
        assert d["success_rate"] == 75 and d["p95_ms"] == 5000
        assert sum(day["requests"] for day in d["trend"]) == 6
        u = d["usage"]
        assert u["input_tokens"] == 15 and u["output_tokens"] == 20
        assert u["unknown_usage_calls"] == 2
        assert u["image_calls"] == 1 and u["pending_calls"] == 1 and u["unpriced_calls"] == 1
        assert u["costs"] == pytest.approx({"CNY": .3, "USD": .2})
        assert Observer(store).analytics(7)["requests"] == 5
        assert Observer(store).analytics(7, project="absent")["success_rate"] is None
    finally:
        store.close()


def test_filter_pagination_and_more_than_fifty_records(tmp_path):
    store = Store(tmp_path / "pages.db")
    try:
        for _ in range(55):
            seed(store, usage={"prompt_tokens": 1, "completion_tokens": 2})
        seed(store, project="other", state="failed", attempt=False)
        observer = Observer(store)
        assert observer.analytics(7, "p")["usage"]["input_tokens"] == 55
        first = observer.requests(7, "p", query="TASK-TAG", limit=20)
        last = observer.requests(7, "p", offset=40)
        assert first["total"] == 55 and len(first["items"]) == 20 and len(last["items"]) == 15
        assert not ({x["id"] for x in first["items"]} & {x["id"] for x in last["items"]})
        assert observer.requests(7, state="failed")["total"] == 1
        assert observer.requests(7, alias="missing")["total"] == 0
        assert observer.requests(7, query="' OR 1=1 --")["total"] == 0
    finally:
        store.close()


def test_observe_query_bounds_and_empty_data(tmp_path):
    with client_for(tmp_path, lambda request: ok()) as client:
        for path in ("analytics?days=31", "analytics?days=0", "requests?offset=-1", "requests?limit=101",
                     "requests?state=nonsense", "requests?query=" + "x" * 201):
            assert client.get("/admin/observe/" + path, headers=ADMIN_AUTH).status_code == 422
        d = client.get("/admin/observe/analytics", headers=ADMIN_AUTH).json()
        assert d["requests"] == 0 and d["p95_ms"] is None and d["success_rate"] is None
        assert d["usage"]["costs"] == {} and d["recent"] == []


def test_old_schema_migration_is_repeatable(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE attempts (id TEXT PRIMARY KEY, operation_id TEXT, project TEXT, deployment TEXT, "
                   "started REAL, elapsed_ms INTEGER, code TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, "
                   "estimated_cost REAL, currency TEXT, cost_status TEXT)")
        db.execute("INSERT INTO attempts(id, deployment) VALUES('historic', 'old-candidate')")
    for _ in range(2):
        store = Store(path)
        try:
            row = store.db.execute("SELECT * FROM attempts WHERE id='historic'").fetchone()
            assert row["deployment"] == "old-candidate" and row["actual_model"] is None
        finally:
            store.close()


def test_attempt_model_is_historical_not_current_configuration(tmp_path):
    with client_for(tmp_path, lambda request: ok()) as client:
        post(client)
        client.app.state.engine.settings.deployments["a"].model = "changed-model"
        d = client.get("/admin/observe/analytics", headers=ADMIN_AUTH).json()
        assert d["groups"]["model"][0]["name"] == "actual-a"
