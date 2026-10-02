import asyncio
import base64
import json
import random
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


class GatewayError(Exception):
    def __init__(self, code, message, status=503, retryable=False, accepted_unknown=False, delay=0):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status
        self.retryable, self.accepted_unknown, self.delay = retryable, accepted_unknown, delay

    def payload(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def retry_after(value):
    try:
        seconds = float(value)
        return max(0, seconds) if seconds < float("inf") else 86400
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class Engine:
    def __init__(self, settings, secrets, store, client):
        self.settings, self.secrets, self.store, self.client = settings, secrets, store, client
        self.semaphores = {"chat": asyncio.Semaphore(settings.text_concurrency),
                           "image": asyncio.Semaphore(settings.image_concurrency)}
        self.probes = set()
        self.tasks = set()

    def scopes(self, name):
        dep = self.settings.deployments[name]
        return ["account:" + dep.account, "key:" + (dep.key_env or name),
                "quota:" + dep.quota_group, "deployment:" + name]

    def select(self, candidates, required, counts):
        blocks = {b["scope"]: b for b in self.store.blocks()}
        for name in sorted(candidates, key=lambda n: counts.get(n, 0)):
            dep = self.settings.deployments[name]
            if not required <= set(dep.capabilities):
                continue
            scopes = self.scopes(name)
            relevant = [blocks[s] for s in scopes if s in blocks and blocks[s]["until"] >= 0]
            if any(b["until"] == 0 or b["until"] > time.time() for b in relevant):
                continue
            expired = {b["scope"] for b in relevant}
            if expired & self.probes:
                continue
            self.probes.update(expired)
            return name, expired
        return None, set()

    def record_failure(self, name, err, probes):
        dep = self.settings.deployments[name]
        if err.code == "UPSTREAM_BILLING_BLOCKED":
            self.store.block("account:" + dep.account, err.code)
        elif err.code == "UPSTREAM_CREDENTIAL_INVALID":
            self.store.block("key:" + (dep.key_env or name), err.code)
        elif err.code == "UPSTREAM_FORBIDDEN":
            self.store.block("deployment:" + name, err.code)
        elif err.code == "UPSTREAM_RATE_LIMITED":
            self.store.block("quota:" + dep.quota_group, err.code,
                             time.time() + max(err.delay, self.settings.retry_base_seconds, 1))
        elif err.retryable:
            self.store.failure("deployment:" + name, self.settings.cooldown_seconds)
        for scope in probes:
            # A failed half-open probe must close the gate again, except hard blocks above.
            state = next((x for x in self.store.blocks() if x["scope"] == scope), None)
            if state and state["until"] != 0 and state["until"] <= time.time():
                self.store.block(scope, "probe_failed", time.time() + self.settings.cooldown_seconds)

    def classify(self, response, dep, kind):
        try:
            detail = response.json().get("error", {})
            code = str(detail.get("code") or detail.get("type") or "") if isinstance(detail, dict) else ""
        except (ValueError, AttributeError):
            code = ""
        if code in dep.billing_error_codes:
            return GatewayError("UPSTREAM_BILLING_BLOCKED", "上游账户余额或付费额度不足。", retryable=True)
        if code.lower() in {"content_policy_violation", "content_filter", "safety_violation"}:
            return GatewayError("CONTENT_REJECTED", "上游拒绝了此内容，请修改请求。", 400)
        if response.status_code == 401:
            return GatewayError("UPSTREAM_CREDENTIAL_INVALID", "上游 Key 已失效。", retryable=True)
        if response.status_code == 403:
            return GatewayError("UPSTREAM_FORBIDDEN", "上游模型未授权，已暂停该候选。", retryable=True)
        if response.status_code == 429:
            unknown = kind == "image" and code not in dep.rejected_image_error_codes
            return GatewayError("UPSTREAM_RATE_LIMITED", "上游暂时限流。", 429, True, unknown,
                                retry_after(response.headers.get("retry-after")))
        if response.status_code >= 500 or response.status_code in {408, 409}:
            return GatewayError("UPSTREAM_UNAVAILABLE", "上游暂时不可用。", 503, True, kind == "image")
        return GatewayError("UPSTREAM_REQUEST_REJECTED", "上游拒绝请求，请检查模型参数和权限。", 400,
                            accepted_unknown=(kind == "image" and code not in dep.rejected_image_error_codes))

    async def invoke(self, name, body, kind, timeout):
        dep = self.settings.deployments[name]
        if dep.provider == "demo":
            if dep.demo_failure == "billing":
                raise GatewayError("UPSTREAM_BILLING_BLOCKED", "演示：主账户余额不足。", retryable=True)
            if dep.demo_failure == "unavailable":
                raise GatewayError("UPSTREAM_UNAVAILABLE", "演示：主接口故障。", retryable=True)
            await asyncio.sleep(0.01)
            if kind == "image":
                # A fixed 1x1 fixture, never represented as a generated business image.
                return {"created": int(time.time()), "data": [{"b64_json":
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="}],
                    "gateway_demo": True}
            content = "【演示数据，未调用付费模型】网关已收到请求，主备切换与记录功能正常。"
            if body.get("response_format", {}).get("type") == "json_object":
                content = json.dumps({"demo": True, "message": content}, ensure_ascii=False)
            return {"id": "demo_" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
                    "model": dep.model, "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                    "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "gateway_demo": True}
        payload = {**dep.defaults, **body, "model": dep.model}
        endpoint = "/images/generations" if kind == "image" else "/chat/completions"
        try:
            async with asyncio.timeout(timeout):
                async with self.client.stream("POST", dep.base_url.rstrip("/") + endpoint,
                    headers={"Authorization": "Bearer " + self.secrets[dep.key_env]}, json=payload,
                    timeout=httpx.Timeout(timeout, connect=min(5, timeout), pool=min(2, timeout))) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 32 * 1024 * 1024:
                            raise GatewayError("UPSTREAM_RESPONSE_TOO_LARGE", "上游结果超过 32MB，请缩小输出。",
                                               502, accepted_unknown=kind == "image")
                        chunks.append(chunk)
                    # aiter_bytes() 已经解压过一轮，这里再带上 content-encoding / content-length
                    # 重建 Response 会让 httpx 按头信息解压第二遍，遇到压缩响应就抛 DecodingError。
                    headers = [(key, value) for key, value in response.headers.multi_items()
                               if key.lower() not in {"content-encoding", "content-length"}]
                    raw = httpx.Response(response.status_code, headers=headers, content=b"".join(chunks))
                    if not 200 <= raw.status_code < 300:
                        raise self.classify(raw, dep, kind)
                    try:
                        result = raw.json()
                    except ValueError:
                        raise GatewayError("OUTPUT_INVALID", "上游返回内容无法解析。", 502, True, kind == "image") from None
                    if not isinstance(result, dict):
                        raise GatewayError("OUTPUT_INVALID", "上游返回结构不正确。", 502, True, kind == "image")
                    return result
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise GatewayError("UPSTREAM_CONNECT_FAILED", "无法建立上游连接。", 503, True) from None
        except (httpx.TimeoutException, TimeoutError):
            raise GatewayError("UPSTREAM_TIMEOUT", "上游调用超时，可能已计费。", 504, True, kind == "image") from None
        except httpx.HTTPError:
            raise GatewayError("UPSTREAM_CONNECTION_LOST", "上游连接中断，可能已计费。", 502, True, kind == "image") from None

    def validate_result(self, result, kind, body, dep):
        if kind == "image":
            try:
                data = result["data"]
                if not isinstance(data, list) or len(data) != 1:
                    raise ValueError()
                encoded = data[0]["b64_json"]
                if not base64.b64decode(encoded, validate=True):
                    raise ValueError()
                return {"created": int(time.time()), "data": [{"b64_json": encoded}], "model": dep.model}
            except (KeyError, ValueError, TypeError, IndexError):
                raise GatewayError("OUTPUT_INVALID", "生图结果不可用，请核对上游任务。", 502, False, True) from None
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "content_filter" or choice["message"].get("refusal"):
                raise GatewayError("CONTENT_REJECTED", "上游拒绝了此内容。", 400)
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
            if body.get("response_format", {}).get("type") == "json_object":
                if not isinstance(json.loads(content), dict):
                    raise ValueError()
            return {"id": str(result.get("id", "chat_" + uuid.uuid4().hex)), "object": "chat.completion",
                    "created": int(time.time()), "model": dep.model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                 "finish_reason": choice.get("finish_reason", "stop")}],
                    "usage": self.safe_usage(result)}
        except (KeyError, ValueError, TypeError, IndexError):
            raise GatewayError("OUTPUT_INVALID", "上游文本或 JSON 格式不正确。", 502, True) from None

    @staticmethod
    def safe_usage(result):
        usage = result.get("usage") if isinstance(result, dict) else None
        if not isinstance(usage, dict):
            return {}
        return {k: v for k, v in usage.items() if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(v, int) and not isinstance(v, bool) and v >= 0}

    def cost(self, result, dep, kind):
        if dep.provider == "demo":
            return 0.0, "demo"
        if kind == "image":
            return (dep.image_price, "estimated") if dep.image_price is not None else (None, "unknown")
        usage = self.safe_usage(result)
        if dep.input_per_million is None or dep.output_per_million is None or not {"prompt_tokens", "completion_tokens"} <= usage.keys():
            return None, "unknown"
        return (usage["prompt_tokens"] * dep.input_per_million + usage["completion_tokens"] * dep.output_per_million) / 1_000_000, "estimated"

    async def execute(self, op, project_id, body, required, requested_timeout=None):
        route = self.settings.routes[body["model"]]
        kind, project = route.kind, self.settings.projects[project_id]
        total = min(route.deadline, requested_timeout) if requested_timeout else route.deadline
        end = asyncio.get_running_loop().time() + total
        sem = self.semaphores[kind]
        acquired, attempt, started, last_dep = False, None, None, None
        try:
            wait = self.settings.text_queue_timeout if kind == "chat" else self.settings.image_queue_timeout
            try:
                await asyncio.wait_for(sem.acquire(), timeout=min(wait, total))
                acquired = True
            except TimeoutError:
                raise GatewayError("GATEWAY_BUSY", "当前请求较多，请稍后重试。", 429) from None
            candidates = [x for x in route.candidates if required <= set(self.settings.deployments[x].capabilities)]
            if not candidates:
                raise GatewayError("MODEL_CAPABILITY_MISMATCH", "该模型池不支持请求所需能力。", 400)
            counts, last_error = {}, None
            for index in range(route.max_attempts):
                remaining = end - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise GatewayError("DEADLINE_EXCEEDED", "已达到请求总时限。", 504)
                name, probes = self.select(candidates, required, counts)
                if name is None:
                    raise last_error or GatewayError("NO_HEALTHY_UPSTREAM", "没有可用主备，请联系管理员查看状态页。")
                dep = self.settings.deployments[name]
                last_dep = dep
                try:
                    attempt = self.store.start_attempt(op, project_id, name, project.daily_limit, dep.model)
                    if not attempt:
                        raise GatewayError("PROJECT_DAILY_LIMIT", "项目今日调用次数已用完，自动切换不能绕过限额。", 429)
                    counts[name] = counts.get(name, 0) + 1
                    started, raw = time.monotonic(), None
                    err = None
                    try:
                        raw = await self.invoke(name, body, kind, min(route.attempt_timeout, remaining))
                        result = self.validate_result(raw, kind, body, dep)
                    except GatewayError as exc:
                        err = exc
                    cost, cost_status = self.cost(raw, dep, kind) if raw is not None else (None, "unknown")
                    if raw is None and err and err.code in {"UPSTREAM_BILLING_BLOCKED", "UPSTREAM_CREDENTIAL_INVALID",
                            "UPSTREAM_FORBIDDEN", "UPSTREAM_CONNECT_FAILED"}:
                        cost, cost_status = 0.0, "not_accepted"
                    self.store.finish_attempt(attempt, time.monotonic()-started, err.code if err else "ok",
                                              dep.currency, self.safe_usage(raw), cost, cost_status)
                    attempt = None
                    if err is None:
                        for scope in probes | {"deployment:" + name}:
                            self.store.clear(scope)
                        result["gateway_demo"] = self.settings.mode == "demo"
                        self.store.finish(op, "succeeded", result=result, model=dep.model,
                                          fallback=name != route.candidates[0])
                        return
                    self.record_failure(name, err, probes)
                    if err.accepted_unknown and kind == "image":
                        raise GatewayError("RESULT_UNKNOWN", "生图可能已经受理，请先核对，勿自动重新生成。", 409,
                                           accepted_unknown=True)
                    if not err.retryable:
                        raise err
                    last_error = err
                finally:
                    self.probes.difference_update(probes)
                if index < route.max_attempts - 1:
                    # Permanent credential/billing failures switch immediately.
                    delay = 0 if last_error.code in {"UPSTREAM_BILLING_BLOCKED", "UPSTREAM_CREDENTIAL_INVALID", "UPSTREAM_FORBIDDEN"} else random.uniform(0, self.settings.retry_base_seconds * 2**index)
                    await asyncio.sleep(min(delay, max(0, end - asyncio.get_running_loop().time())))
            raise last_error or GatewayError("NO_HEALTHY_UPSTREAM", "没有可用上游。")
        except GatewayError as err:
            self.store.finish(op, "unknown" if err.accepted_unknown else "failed", error=err.payload(), status=err.status)
        except asyncio.CancelledError:
            if attempt and last_dep:
                self.store.finish_attempt(attempt, time.monotonic() - started, "RESULT_UNKNOWN", last_dep.currency)
            self.store.finish(op, "unknown", error=GatewayError("RESULT_UNKNOWN", "服务中断，请核对上游结果。", 409).payload(), status=409)
            raise
        except Exception:
            if attempt and last_dep:
                self.store.finish_attempt(attempt, time.monotonic() - started, "RESULT_UNKNOWN", last_dep.currency)
            self.store.finish(op, "unknown", error=GatewayError("INTERNAL_ERROR", "网关异常，请联系管理员核对操作。", 503).payload(), status=503)
        finally:
            if acquired:
                sem.release()

    def launch(self, *args):
        task = asyncio.create_task(self.execute(*args))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
