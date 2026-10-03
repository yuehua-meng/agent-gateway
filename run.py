import os

import uvicorn
from gateway.config import load_secrets, load_settings
from gateway.main import create_app

if __name__ == "__main__":
    settings = load_settings()
    app = create_app(settings, load_secrets(settings))
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("GATEWAY_PORT", "8020"))
    print(f"Agent gateway: http://{host}:{port} | mode={settings.mode} | single worker")
    uvicorn.run(app, host=host, port=port, workers=1, access_log=False,
                timeout_graceful_shutdown=300,
                limit_concurrency=max(256, settings.text_concurrency + settings.image_concurrency + settings.image_queue_limit + 64))
