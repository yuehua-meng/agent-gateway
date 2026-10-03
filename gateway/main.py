import asyncio
import hmac
import json
import math
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .admission import CapacityFull
from .config import ROOT, load_secrets, load_settings
from .engine import Engine, GatewayError
from .observe import Observer
from .storage import Store


class SingleProcessLock:
    def __init__(self, path):
        self.file = open(path, "a+b")
        try:
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.file.close()
            raise RuntimeError("This gateway data directory is already in use. Run one worker only.") from None

    def close(self):
        self.file.close()


async def read_body(request, limit=8*1024*1024):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise GatewayError("REQUEST_TOO_LARGE", "请求超过大小限制。", 413)
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeError):
        raise GatewayError("INVALID_REQUEST", "请求需要合法 JSON。", 400) from None
    if not isinstance(obj, dict):
        raise GatewayError("INVALID_REQUEST", "请求需要 JSON 对象。", 400)
    return obj


def validate_body(body, kind, project):
    common = {"model"}
    fields = {"messages", "max_tokens", "temperature", "top_p", "response_format", "stream", "stop"} if kind == "chat" else {
        "prompt", "image", "size", "response_format", "n"}
    if set(body) - common - fields:
        raise GatewayError("UNSUPPORTED_PARAMETER", "存在暂不支持的参数；仅支持文档中列出的接口子集。", 400)
    required = {"text"} if kind == "chat" else {"image"}
    if kind == "chat":
        if body.get("stream", False) is not False:
            raise GatewayError("STREAM_NOT_SUPPORTED", "第一版仅支持 stream=false。", 400)
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 200:
            raise GatewayError("INVALID_REQUEST", "messages 需要包含 1–200 条消息。", 400)
        for message in messages:
            if not isinstance(message, dict) or set(message) - {"role", "content"} or message.get("role") not in ("system", "user", "assistant"):
                raise GatewayError("INVALID_REQUEST", "消息仅支持 system/user/assistant 和 content。", 400)
            content = message.get("content")
            if isinstance(content, str) and content:
                continue
            if not isinstance(content, list) or not content:
                raise GatewayError("INVALID_REQUEST", "消息内容不能为空。", 400)
            for part in content:
                if not isinstance(part, dict):
                    raise GatewayError("INVALID_REQUEST", "无效的消息内容。", 400)
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    if set(part) - {"type", "text"}:
                        raise GatewayError("INVALID_REQUEST", "不支持的文本字段。", 400)
                elif part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
                    if set(part) - {"type", "image_url"} or set(part["image_url"]) - {"url", "detail"}:
                        raise GatewayError("INVALID_REQUEST", "不支持的图片字段。", 400)
                    validate_image(part["image_url"].get("url"))
                    required.add("vision")
                else:
                    raise GatewayError("INVALID_REQUEST", "只支持 text 和 image_url 内容。", 400)
        tokens = body.get("max_tokens", project.max_output_tokens)
        if type(tokens) is not int or not 1 <= tokens <= project.max_output_tokens:
            raise GatewayError("OUTPUT_LIMIT_EXCEEDED", "max_tokens 超出该项目允许范围。", 400)
        body["max_tokens"] = tokens
        for name, high in (("temperature", 2), ("top_p", 1)):
            if name in body and (type(body[name]) not in {int, float} or not math.isfinite(body[name]) or not 0 <= body[name] <= high):
                raise GatewayError("INVALID_REQUEST", f"{name} 数值不正确。", 400)
        fmt = body.get("response_format")
        if fmt is not None:
            if fmt not in ({"type": "text"}, {"type": "json_object"}):
                raise GatewayError("INVALID_REQUEST", "response_format 仅支持 text 或 json_object。", 400)
            if fmt["type"] == "json_object":
                required.add("json")
        if "stop" in body and not (isinstance(body["stop"], str) or (
            isinstance(body["stop"], list) and len(body["stop"]) <= 4 and all(isinstance(x, str) for x in body["stop"]))):
            raise GatewayError("INVALID_REQUEST", "stop 需要字符串或最多四个字符串。", 400)
    else:
        if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
            raise GatewayError("INVALID_REQUEST", "prompt 不能为空。", 400)
        if type(body.get("n", 1)) is not int or body.get("n", 1) != 1 or body.get("response_format", "b64_json") != "b64_json":
            raise GatewayError("INVALID_REQUEST", "生图只支持 n=1、response_format=b64_json。", 400)
        body["n"], body["response_format"] = 1, "b64_json"
        if "size" in body and (not isinstance(body["size"], str) or len(body["size"]) > 30):
            raise GatewayError("INVALID_REQUEST", "size 应为供应商支持的尺寸字符串。", 400)
        if "image" in body:
            images = body["image"] if isinstance(body["image"], list) else [body["image"]]
            if not 1 <= len(images) <= 4:
                raise GatewayError("INVALID_REQUEST", "参考图片数量须为 1–4。", 400)
            for value in images:
                validate_image(value)
            required.add("image_to_image")
            if len(images) > 1:
                required.add("multiple_reference_images")
    return required


def validate_image(value):
    # No URL fetching anywhere in the gateway; inline images avoid SSRF and expiring URLs.
    import base64
    if not isinstance(value, str) or not any(value.startswith(f"data:image/{fmt};base64,") for fmt in ("png", "jpeg", "webp")):
        raise GatewayError("INVALID_IMAGE", "第一版图片输入仅支持 PNG/JPEG/WebP base64 data URL。", 400)
    try:
        if not base64.b64decode(value.split(",", 1)[1], validate=True):
            raise ValueError()
    except ValueError:
        raise GatewayError("INVALID_IMAGE", "图片 base64 不正确。", 400) from None


def create_app(settings=None, secrets=None, data_dir=None, transport=None):
    settings = settings or load_settings()
    secrets = secrets or load_secrets(settings)
    data_dir = Path(data_dir) if data_dir else ROOT / "data"

    @asynccontextmanager
    async def lifespan(app):
        data_dir.mkdir(parents=True, exist_ok=True)
        lock = SingleProcessLock(data_dir / ".process.lock")
        store = None
        try:
            store = Store(data_dir / "gateway.db")
            store.recover()
            async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False,
                    limits=httpx.Limits(max_connections=settings.text_concurrency+settings.image_concurrency,
                                       max_keepalive_connections=settings.text_concurrency+settings.image_concurrency)) as client:
                engine = Engine(settings, secrets, store, client)
                app.state.store, app.state.engine = store, engine

                async def maintenance():
                    last_day = None
                    while True:
                        from .storage import today
                        day = today()
                        if day != last_day:
                            store.cleanup()
                            backups = data_dir / "backups"
                            backups.mkdir(exist_ok=True)
                            store.backup(backups / f"gateway-{day}.db")
                            last_day = day
                            for old in sorted(backups.glob("gateway-*.db"))[:-7]:
                                old.unlink()
                        await asyncio.sleep(60)

                maintenance_task = asyncio.create_task(maintenance())
                try:
                    yield
                finally:
                    maintenance_task.cancel()
                    await asyncio.gather(maintenance_task, return_exceptions=True)
                    await engine.close()
        finally:
            if store:
                store.close()
            lock.close()

    app = FastAPI(title="小团队 Agent 网关", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def safety_headers(request, call_next):
        try:
            response = await call_next(request)
        except sqlite3.Error:
            response = JSONResponse({"error": {"code": "STORAGE_UNAVAILABLE", "message": "网关存储不可用，已停止新付费调用。", "retryable": False}}, status_code=503)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        return JSONResponse({"error": exc.payload()}, status_code=exc.status)

    def token(request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise GatewayError("UNAUTHORIZED", "请提供项目或管理员访问凭据。", 401)
        return auth[7:]

    def project_auth(request):
        value = token(request)
        for name, project in settings.projects.items():
            if hmac.compare_digest(value.encode(), secrets[project.key_env].encode()):
                return name
        raise GatewayError("UNAUTHORIZED", "项目凭据无效。", 401)

    def admin_auth(request):
        if not hmac.compare_digest(token(request).encode(), secrets["GATEWAY_ADMIN_KEY"].encode()):
            raise GatewayError("FORBIDDEN", "需要独立的管理员凭据。", 403)

    def respond(op):
        headers = {"X-Gateway-Request-Id": op["id"], "X-Gateway-Demo": str(settings.mode == "demo").lower(),
                   "X-Fallback-Used": str(bool(op["fallback"])).lower()}
        if op["served_model"]:
            # Model identifiers are configured by the admin; prevent invalid HTTP headers.
            headers["X-Served-Model"] = op["served_model"].encode("ascii", "replace").decode()
        if op["state"] == "succeeded":
            return JSONResponse(json.loads(op["result"]), headers=headers)
        if op["state"] == "running":
            return JSONResponse({"operation_id": op["id"], "status": "running", "poll_url": "/operations/" + op["id"]},
                                status_code=202, headers=headers)
        if op["state"] == "expired":
            return JSONResponse({"error": {"code": "RESULT_EXPIRED", "message": "结果已过期，此幂等键不会重新提交。",
                                           "operation_id": op["id"], "retryable": False}}, status_code=410, headers=headers)
        error = json.loads(op["error"]) if op["error"] else {"code": "RESULT_UNKNOWN", "retryable": False}
        error["operation_id"] = op["id"]
        return JSONResponse({"error": error}, status_code=op["http_status"] or 409, headers=headers)

    async def submit(request, kind):
        project_id = project_auth(request)
        project = settings.projects[project_id]
        body = await read_body(request)
        alias = body.get("model")
        if not isinstance(alias, str) or alias not in project.models:
            raise GatewayError("MODEL_FORBIDDEN", "此项目无权调用该模型别名。", 403)
        route = settings.routes[alias]
        if route.kind != kind:
            raise GatewayError("INVALID_ENDPOINT", "模型类型与调用接口不匹配。", 400)
        required = validate_body(body, kind, project)
        timeout = None
        if "x-timeout-ms" in request.headers:
            try:
                timeout = float(request.headers["x-timeout-ms"]) / 1000
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError()
            except ValueError:
                raise GatewayError("INVALID_REQUEST", "X-Timeout-Ms 需要正数。", 400) from None
        idem = request.headers.get("idempotency-key")
        if kind == "image" and not idem:
            raise GatewayError("IDEMPOTENCY_KEY_REQUIRED", "生图必须提供唯一 Idempotency-Key，同一次操作重发时复用。", 400)
        idem = idem or "auto_" + uuid.uuid4().hex
        task_id = request.headers.get("x-task-id", "")
        if len(idem) > 200 or len(task_id) > 200:
            raise GatewayError("INVALID_REQUEST", "操作标识或任务标识过长。", 400)
        engine, store = app.state.engine, app.state.store
        ticket = None
        def admit():
            nonlocal ticket
            try:
                ticket = engine.admission.reserve(kind)
            except CapacityFull:
                raise GatewayError("GATEWAY_BUSY", "当前执行名额或图片等待队列已满，请稍后重试。",
                                   429, retryable=True) from None
        try:
            # Existing operations bypass admission. Claim and reservation contain no await.
            op, fresh, matches = store.claim(project_id, idem, alias, kind, body, task_id, admit=admit)
        except BaseException:
            if ticket:
                engine.admission.release(ticket)
            raise
        if not matches:
            raise GatewayError("IDEMPOTENCY_CONFLICT", "相同幂等键不能提交不同内容。", 409)
        if fresh:
            # Shield accepted work from an HTTP client disconnect; bounded by its original deadline.
            await asyncio.shield(engine.launch(op["id"], project_id, body, required, timeout, ticket=ticket))
            op = store.operation(op["id"], project_id)
        return respond(op)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await submit(request, "chat")

    @app.post("/v1/images/generations")
    async def images(request: Request):
        return await submit(request, "image")

    @app.get("/operations/{operation_id}")
    async def operation(operation_id: str, request: Request):
        op = app.state.store.operation(operation_id, project_auth(request))
        if not op:
            raise GatewayError("NOT_FOUND", "操作不存在。", 404)
        return respond(op)

    @app.get("/v1/models")
    async def models(request: Request):
        project = settings.projects[project_auth(request)]
        return {"object": "list", "data": [{"id": x, "object": "model", "owned_by": "team-gateway"} for x in project.models]}

    @app.get("/health")
    async def health():
        app.state.store.db.execute("SELECT 1")
        return {"status": "ok", "mode": settings.mode, "version": "0.1.0"}

    @app.get("/admin/status")
    async def status(request: Request):
        admin_auth(request)
        return {"mode": settings.mode, "active_requests": len(app.state.engine.tasks),
                "capacity": app.state.engine.admission.snapshot(),
                "projects": {n: {"models": p.models, "daily_limit": p.daily_limit} for n, p in settings.projects.items()},
                "deployments": {n: {"model": d.model, "account": d.account, "quota_group": d.quota_group,
                                      "scopes": app.state.engine.scopes(n)} for n, d in settings.deployments.items()},
                **app.state.store.overview()}

    @app.post("/admin/reset-block")
    async def reset_block(request: Request):
        admin_auth(request)
        body = await read_body(request, 4096)
        scope = body.get("scope")
        if not isinstance(scope, str) or scope not in {b["scope"] for b in app.state.store.blocks()}:
            raise GatewayError("NOT_FOUND", "停用记录不存在。", 404)
        app.state.store.clear(scope, audited=True)
        return {"status": "reset", "scope": scope}

    @app.get("/admin/observe/analytics")
    def analytics(request: Request, days: int = Query(7, ge=1, le=30),
                  project: str = Query("", max_length=200), alias: str = Query("", max_length=200)):
        admin_auth(request)
        return Observer(app.state.store).analytics(days, project, alias)

    @app.get("/admin/observe/requests")
    def observed_requests(request: Request, days: int = Query(7, ge=1, le=30),
                          project: str = Query("", max_length=200), alias: str = Query("", max_length=200),
                          state: str = Query("", pattern="^(|succeeded|failed|running|unknown)$"),
                          query: str = Query("", max_length=200), offset: int = Query(0, ge=0),
                          limit: int = Query(20, ge=1, le=100)):
        admin_auth(request)
        return Observer(app.state.store).requests(days, project, alias, state, query, offset, limit)

    @app.get("/admin/observe/requests/{operation_id}")
    def observed_detail(operation_id: str, request: Request):
        admin_auth(request)
        detail = Observer(app.state.store).detail(operation_id)
        if detail is None:
            raise GatewayError("NOT_FOUND", "操作不存在。", 404)
        return detail

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    @app.get("/static/{name}")
    async def static(name: str):
        from fastapi.responses import Response
        if name not in {"app.js", "style.css", "icon.svg"}:
            raise GatewayError("NOT_FOUND", "不存在。", 404)
        return Response((ROOT / "web" / name).read_text(encoding="utf-8"),
                        media_type={"app.js": "text/javascript", "style.css": "text/css", "icon.svg": "image/svg+xml"}[name])

    return app
