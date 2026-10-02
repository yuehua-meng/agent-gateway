"""Safe demo smoke test; live calls require an explicit --live flag."""
import argparse
import os
import uuid

import httpx
from gateway.config import load_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="allow a paid minimal text call")
    parser.add_argument("--project", default=None)
    args = parser.parse_args()
    settings = load_settings()
    if settings.mode == "live" and not args.live:
        raise SystemExit("Live mode: pass --live only when you intend a paid test call.")
    project_id = args.project or next(iter(settings.projects))
    project = settings.projects[project_id]
    alias = next((x for x in project.models if settings.routes[x].kind == "chat"), None)
    if not alias:
        raise SystemExit("Project has no chat model.")
    port = os.getenv("GATEWAY_PORT", "8020")
    response = httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", headers={
        "Authorization": "Bearer " + os.environ[project.key_env], "Idempotency-Key": "smoke-" + uuid.uuid4().hex},
        json={"model": alias, "messages": [{"role": "user", "content": "请回复：连接正常"}],
              "max_tokens": min(64, project.max_output_tokens)}, timeout=130)
    print("HTTP:", response.status_code, "fallback:", response.headers.get("x-fallback-used"))
    print(response.text)
    response.raise_for_status()


if __name__ == "__main__":
    main()
