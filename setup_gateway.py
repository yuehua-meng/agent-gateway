"""Initialize only this directory; never read the parent project's .env."""
import secrets
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    target = ROOT / "config.json"
    if not target.exists():
        shutil.copyfile(ROOT / "config.example.json", target)
    target = ROOT / ".env"
    if not target.exists():
        target.write_text(
            "# Generated local credentials. Do not commit or share the admin key.\n"
            f"GATEWAY_ADMIN_KEY={secrets.token_urlsafe(32)}\n"
            f"PROJECT_DEMO_KEY={secrets.token_urlsafe(32)}\n"
            "GATEWAY_HOST=127.0.0.1\nGATEWAY_PORT=8020\n", encoding="utf-8")
    print("Ready. Config: config.json. Access keys: .env (not printed).")
    print("New configurations use DEMO mode. Existing configurations are preserved. Start with python run.py")


if __name__ == "__main__":
    main()
