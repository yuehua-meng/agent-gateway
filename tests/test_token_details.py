import pytest
from gateway.engine import Engine
from gateway.observe import Observer
from gateway.storage import Store
from test_gateway import client_for, ok, post
from test_observe import ADMIN_AUTH, seed


def test_details_flow_without_double_counting_or_cost_change(tmp_path):
    def response(request):
        result = ok()
        body = result.json()
        body["usage"]["prompt_tokens_details"] = {"cached_tokens": 6, "secret": "do not forward"}
        body["usage"]["completion_tokens_details"] = {"reasoning_tokens": 12}
        return type(result)(200, json=body)

    with client_for(tmp_path, response) as client:
        r = post(client, key="tokens-idem")
        u = r.json()["usage"]
        assert u["total_tokens"] == 30
        assert u["prompt_tokens_details"] == {"cached_tokens": 6}
        assert u["completion_tokens_details"] == {"reasoning_tokens": 12}
        assert post(client, key="tokens-idem").json() == r.json()
        d = client.get("/admin/observe/analytics", headers=ADMIN_AUTH).json()
        for u in [d["usage"], d["groups"]["project"][0], d["groups"]["model"][0], d["recent"][0]["usage"]]:
            assert u["input_tokens"] + u["output_tokens"] == 30
            assert u["cached_tokens"] == 6 and u["reasoning_tokens"] == 12
            assert u["cached_known_calls"] == u["reasoning_known_calls"] == 1
            assert u["cached_unknown_calls"] == u["reasoning_unknown_calls"] == 0
        detail = client.get('/admin/observe/requests/' + r.headers['x-gateway-request-id'], headers=ADMIN_AUTH).json()
        a = detail["attempts"][0]
        assert a["cached_tokens"] == 6 and a["reasoning_tokens"] == 12
        assert a["estimated_cost"] == pytest.approx(.00005)


@pytest.mark.parametrize("value", [None, -1, True, 1.5, "5", 2**70, 21])
def test_invalid_detail_counts_are_unknown(value):
    clean = Engine.safe_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 20,
                                       "prompt_tokens_details": {"cached_tokens": value},
                                       "completion_tokens_details": {"reasoning_tokens": value}}})
    assert clean == {"prompt_tokens": 10, "completion_tokens": 20}


@pytest.mark.parametrize("details", [None, [], "invalid", 5, {"unrelated": 3}])
def test_malformed_detail_objects_do_not_fail_request(details):
    assert Engine.safe_usage({"usage": {"prompt_tokens_details": details,
                                       "completion_tokens_details": details}}) == {}


def test_known_zero_missing_images_and_running_are_distinct(tmp_path):
    store = Store(tmp_path / 'details.db')
    try:
        zero = {"prompt_tokens": 10, "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0}}
        seed(store, usage=zero)
        seed(store, usage={"prompt_tokens": 10, "completion_tokens": 20})
        seed(store, kind='image', usage={})
        seed(store, state='running', code='running', usage={})
        u = Observer(store).analytics(7)["usage"]
        assert u["cached_tokens"] == u["reasoning_tokens"] == 0
        assert u["cached_known_calls"] == u["reasoning_known_calls"] == 1
        assert u["cached_unknown_calls"] == u["reasoning_unknown_calls"] == 1
        assert u["unknown_usage_calls"] == 0
        assert u["input_tokens"] + u["output_tokens"] == 60
    finally:
        store.close()


def test_old_rows_remain_unknown_after_migration(tmp_path):
    path = tmp_path / 'old-details.db'
    store = Store(path)
    op = seed(store, usage={"prompt_tokens": 10, "completion_tokens": 20})
    # Simulate the schema before this addition, keeping existing data.
    store.db.execute('ALTER TABLE attempts DROP COLUMN cached_tokens')
    store.db.execute('ALTER TABLE attempts DROP COLUMN reasoning_tokens')
    store.close()
    for _ in range(2):
        store = Store(path)
        try:
            d = Observer(store).detail(op)
            assert d['attempts'][0]['cached_tokens'] is None
            assert d['attempts'][0]['reasoning_tokens'] is None
            assert d['operation']['usage']['cached_unknown_calls'] == 1
        finally:
            store.close()
