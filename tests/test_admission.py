import asyncio

import httpx
import pytest
from gateway.admission import Admission, CapacityFull
from gateway.config import Settings
from gateway.main import create_app
from test_gateway import ADMIN, AUTH, BODY, PIXEL, SECRETS, configuration, ok


async def until(predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(.005)


def configured(**changes):
    config = configuration()
    config.update(changes)
    config['projects']['first']['daily_limit'] = 1000
    for route in config['routes'].values():
        route.update(attempt_timeout=20, deadline=30)
    return Settings.model_validate(config)


def test_200_text_10_images_6_waiting_and_idempotent_replay(tmp_path):
    async def run():
        release = asyncio.Event()
        calls = {'chat': 0, 'image': 0}

        async def upstream(req):
            kind = 'image' if req.url.path.endswith('generations') else 'chat'
            calls[kind] += 1
            await release.wait()
            return httpx.Response(200, json={'data': [{'b64_json': PIXEL}]}) if kind == 'image' else ok()

        app = create_app(configured(), SECRETS, tmp_path, httpx.MockTransport(upstream))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://gateway') as client:
                async def request(kind, key, **extra):
                    body = BODY if kind == 'chat' else {'model': 'image.default', 'prompt': 'test'}
                    path = '/v1/chat/completions' if kind == 'chat' else '/v1/images/generations'
                    return await client.post(path, json={**body, **extra}, headers={**AUTH, 'Idempotency-Key': key})

                tasks = [asyncio.create_task(request('chat', f't-{i}')) for i in range(200)]
                await until(lambda: calls['chat'] == 200)
                tasks += [asyncio.create_task(request('image', f'i-{i}')) for i in range(10)]
                await until(lambda: calls['image'] == 10)
                tasks += [asyncio.create_task(request('image', f'q-{i}')) for i in range(6)]
                await until(lambda: len(app.state.engine.admission.waiting) == 6)
                snapshot = (await client.get('/admin/status', headers={'Authorization': 'Bearer ' + ADMIN})).json()
                assert snapshot['capacity'] == {'text_active': 200, 'text_limit': 200, 'image_active': 10,
                                               'image_limit': 10, 'image_waiting': 6, 'image_queue_limit': 6}
                assert (await request('chat', 'overflow-text')).status_code == 429
                assert (await request('image', 'overflow-image')).status_code == 429
                assert (await request('chat', 't-0')).status_code == 202
                assert (await request('image', 'q-0')).status_code == 202
                assert (await request('chat', 't-0', temperature=.1)).status_code == 409
                assert calls == {'chat': 200, 'image': 10}
                release.set()
                results = await asyncio.gather(*tasks)
                assert all(r.status_code == 200 for r in results)
                assert calls == {'chat': 200, 'image': 16}
                # Admission rejection did not poison the idempotency keys.
                assert (await request('chat', 'overflow-text')).status_code == 200
                assert (await request('image', 'overflow-image')).status_code == 200
                assert app.state.engine.admission.active == {'chat': 0, 'image': 0}
                assert not app.state.engine.admission.waiting

    asyncio.run(run())


def test_image_queue_timeout_same_key_can_retry_without_charging(tmp_path):
    async def run():
        release = asyncio.Event()
        calls = []

        async def upstream(req):
            calls.append(req)
            if req.url.path.endswith('generations'):
                await release.wait()
                return httpx.Response(200, json={'data': [{'b64_json': PIXEL}]})
            return ok()

        app = create_app(configured(image_concurrency=1, image_queue_limit=1, image_queue_timeout=.05),
                         SECRETS, tmp_path, httpx.MockTransport(upstream))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://gateway') as client:
                async def image(key):
                    return await client.post('/v1/images/generations', json={'model': 'image.default', 'prompt': 'test'},
                                             headers={**AUTH, 'Idempotency-Key': key})
                first = asyncio.create_task(image('first'))
                await until(lambda: len(calls) == 1)
                waiting = asyncio.create_task(image('waiting'))
                await until(lambda: len(app.state.engine.admission.waiting) == 1)
                assert (await image('full')).status_code == 429
                # An image backlog must not block text with available capacity.
                assert (await client.post('/v1/chat/completions', headers=AUTH, json=BODY)).status_code == 200
                rejected = await waiting
                assert rejected.status_code == 429 and rejected.json()['error']['retryable']
                assert len(calls) == 2
                op_id = rejected.headers['x-gateway-request-id']
                assert app.state.store.db.execute('SELECT COUNT(*) FROM attempts WHERE operation_id=?', (op_id,)).fetchone()[0] == 0
                release.set()
                assert (await first).status_code == 200
                retried = await image('waiting')
                assert retried.status_code == 200 and retried.headers['x-gateway-request-id'] == op_id
                assert len(calls) == 3
                assert app.state.engine.admission.active == {'chat': 0, 'image': 0}

    asyncio.run(run())


def test_image_fifo_cancellation_and_release_are_safe():
    async def run():
        gate = Admission(200, 1, 2)
        first = gate.reserve('image')
        second = gate.reserve('image')
        third = gate.reserve('image')
        with pytest.raises(CapacityFull):
            gate.reserve('image')
        gate.release(second)
        assert second.ready.cancelled()
        gate.release(first)
        assert third.active and third.ready.done()
        gate.release(first)
        assert gate.active['image'] == 1
        gate.release(third)
        assert gate.active['image'] == 0
    asyncio.run(run())


def test_failure_during_claim_releases_reserved_slot(tmp_path):
    from test_gateway import client_for, post
    with client_for(tmp_path, lambda request: ok()) as client:
        # Reject the INSERT after admission has reserved a slot.
        client.app.state.store.db.execute("CREATE TRIGGER reject_operation BEFORE INSERT ON operations "
                                         "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        assert post(client).status_code == 503
        assert client.app.state.engine.admission.active == {'chat': 0, 'image': 0}
        client.app.state.store.db.execute('DROP TRIGGER reject_operation')
        assert post(client).status_code == 200


def test_cancel_before_execution_releases_ticket(tmp_path):
    async def run():
        app = create_app(configured(), SECRETS, tmp_path, httpx.MockTransport(lambda req: ok()))
        async with app.router.lifespan_context(app):
            engine = app.state.engine
            ticket = engine.admission.reserve('chat')
            op, _, _ = app.state.store.claim('first', 'cancelled-before-start', 'text.default', 'chat', BODY, '')
            task = engine.launch(op['id'], 'first', BODY, {'text'}, ticket=ticket)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert engine.admission.active['chat'] == 0
            assert not engine.tasks
    asyncio.run(run())


def test_image_wait_is_bounded_by_request_deadline(tmp_path):
    async def run():
        app = create_app(configured(image_concurrency=1), SECRETS, tmp_path, httpx.MockTransport(lambda req: ok()))
        async with app.router.lifespan_context(app):
            engine = app.state.engine
            occupied = engine.admission.reserve('image')
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://gateway') as client:
                    async with asyncio.timeout(1):
                        r = await client.post('/v1/images/generations',
                                              headers={**AUTH, 'Idempotency-Key': 'short', 'X-Timeout-Ms': '20'},
                                              json={'model': 'image.default', 'prompt': 'test'})
                    assert r.status_code == 429
                    assert not engine.admission.waiting
                    assert not app.state.store.overview()['daily']
            finally:
                engine.admission.release(occupied)
    asyncio.run(run())
