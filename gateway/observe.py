"""Read-only dashboard queries. Never select stored results, keys or prompt bodies."""

import math
import time
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
OP_FIELDS = ("id,project,alias,kind,task_id,state,created,updated,http_status,served_model,fallback,"
             "json_extract(error,'$.code') AS error_code")
ATT_FIELDS = ("a.id,a.operation_id,a.project,a.deployment,a.actual_model,a.started,a.elapsed_ms,"
              "a.code,a.prompt_tokens,a.completion_tokens,a.estimated_cost,a.currency,a.cost_status")


def usage():
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "known_token_calls": 0,
            "unknown_usage_calls": 0, "image_calls": 0, "pending_calls": 0,
            "unpriced_calls": 0, "not_accepted_calls": 0, "costs": {}}


def add_usage(total, attempt, kind):
    total["calls"] += 1
    if attempt["code"] == "running":
        total["pending_calls"] += 1
    if kind == "image":
        total["image_calls"] += 1
    elif attempt["code"] != "running":
        inp, out = attempt["prompt_tokens"], attempt["completion_tokens"]
        total["input_tokens"] += inp or 0
        total["output_tokens"] += out or 0
        if inp is None or out is None:
            total["unknown_usage_calls"] += 1
        else:
            total["known_token_calls"] += 1
    cost = attempt["estimated_cost"]
    if attempt["cost_status"] == "not_accepted":
        total["not_accepted_calls"] += 1
    elif cost is not None and attempt["currency"]:
        currency = attempt["currency"]
        total["costs"][currency] = total["costs"].get(currency, 0) + cost
    elif attempt["code"] != "running":
        total["unpriced_calls"] += 1


def public_operation(row, now):
    row = dict(row)
    # expired means its successful result cache expired, not that the call failed.
    row["result_expired"] = row["state"] == "expired"
    if row["result_expired"]:
        row["state"] = "succeeded"
    row["duration_ms"] = round(max(0, (now if row["state"] == "running" else row["updated"]) - row["created"]) * 1000)
    return row


class Observer:
    def __init__(self, store):
        self.store = store

    def snapshot(self, days, project="", alias=""):
        now = time.time()
        where, args = "created>=? AND created<=?", [now - days * 86400, now]
        for field, value in (("project", project), ("alias", alias)):
            if value:
                where += f" AND {field}=?"
                args.append(value)
        # One short, consistent metadata read. Aggregation happens outside the write lock.
        with self.store.lock:
            operations = [public_operation(r, now) for r in self.store.db.execute(
                f"SELECT {OP_FIELDS} FROM operations WHERE {where} ORDER BY created DESC,id", args)]
            attempts = [dict(r) for r in self.store.db.execute(
                f"SELECT {ATT_FIELDS} FROM attempts a JOIN (SELECT id FROM operations WHERE {where}) o "
                "ON a.operation_id=o.id ORDER BY a.started,a.id", args)]
        by_id = {op["id"]: op for op in operations}
        for op in operations:
            op["usage"] = usage()
            op["attempts"] = []
        for attempt in attempts:
            op = by_id[attempt["operation_id"]]
            op["attempts"].append(attempt)
            add_usage(op["usage"], attempt, op["kind"])
        return now, operations

    def analytics(self, days, project="", alias=""):
        now, operations = self.snapshot(days, project, alias)
        total = usage()
        groups = {"project": {}, "model": {}, "deployment": {}}
        first = datetime.fromtimestamp(now - days * 86400, TZ).date()
        last = datetime.fromtimestamp(now, TZ).date()
        trend = {}
        for offset in range((last - first).days + 1):
            day = (first + timedelta(days=offset)).isoformat()
            trend[day] = {"date": day, "requests": 0, "failed": 0}
        counts = {s: 0 for s in ("succeeded", "failed", "running", "unknown")}
        durations = []
        retries = 0
        for op in operations:
            counts[op["state"]] += 1
            day = datetime.fromtimestamp(op["created"], TZ).date().isoformat()
            trend[day]["requests"] += 1
            trend[day]["failed"] += op["state"] == "failed"
            if op["state"] in {"succeeded", "failed"}:
                durations.append(op["duration_ms"])
            retries += max(0, len(op["attempts"]) - 1)
            for attempt in op["attempts"]:
                add_usage(total, attempt, op["kind"])
                for dimension, name in (("project", op["project"]),
                                        ("model", attempt["actual_model"] or "历史模型未记录"),
                                        ("deployment", attempt["deployment"])):
                    group = groups[dimension].setdefault(name, {"name": name, **usage()})
                    add_usage(group, attempt, op["kind"])
        durations.sort()
        terminal = counts["succeeded"] + counts["failed"]
        return {"requests": len(operations), **counts, "usage": total, "retries": retries,
                "success_rate": round(counts["succeeded"] / terminal * 100, 2) if terminal else None,
                "p95_ms": durations[math.ceil(len(durations) * .95) - 1] if durations else None,
                "trend": list(trend.values()), "groups": {k: sorted(v.values(), key=lambda g: -g["calls"])
                                                          for k, v in groups.items()},
                "recent": [self.summary(op) for op in operations[:8]], "as_of": now}

    @staticmethod
    def summary(op):
        return {k: v for k, v in op.items() if k != "attempts"} | {"attempt_count": len(op["attempts"])}

    def requests(self, days, project="", alias="", state="", query="", offset=0, limit=20):
        _, operations = self.snapshot(days, project, alias)
        query = query.casefold()
        items = [op for op in operations if (not state or op["state"] == state) and (not query or
                 any(query in str(op[k] or "").casefold() for k in ("id", "task_id", "project", "alias")))]
        return {"total": len(items), "items": [self.summary(op) for op in items[offset:offset + limit]],
                "offset": offset, "limit": limit}

    def detail(self, operation_id):
        with self.store.lock:
            row = self.store.db.execute(f"SELECT {OP_FIELDS} FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                return None
            attempts = [dict(r) for r in self.store.db.execute(
                f"SELECT {ATT_FIELDS} FROM attempts a WHERE operation_id=? ORDER BY started,id", (operation_id,))]
        op = public_operation(row, time.time())
        op["usage"] = usage()
        for attempt in attempts:
            add_usage(op["usage"], attempt, op["kind"])
        return {"operation": op, "attempts": attempts}
