"""Generate a private configuration without echoing credentials; standard library only."""
import base64
import getpass
import os
from pathlib import Path
import re
import secrets


def main():
    destination = Path(__file__).resolve().parents[1] / ".env"
    if destination.exists():
        raise SystemExit(".env already exists; edit it manually. Encryption key was NOT replaced.")
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Invalid Telegram bot token")
    owners = input("Bot owner Telegram numeric user IDs (comma-separated): ").strip()
    if not re.fullmatch(r"[1-9][0-9]*(\s*,\s*[1-9][0-9]*)*", owners):
        raise SystemExit("Invalid user IDs")
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as config:
        config.write(f"TELEGRAM_BOT_TOKEN={token}\nALLOWED_USER_IDS={owners.replace(' ', '')}\n"
                     f"ENCRYPTION_KEY={key}\nDATA_DIR=./data\n")
    print("Created .env. Back up this file securely: losing ENCRYPTION_KEY makes saved tokens unreadable.")


if __name__ == "__main__":
    main()
