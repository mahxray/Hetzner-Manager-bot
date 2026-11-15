"""Import a token on the VPS without sending it through Telegram."""
import argparse
import asyncio
import getpass
import os
import sqlite3

import httpx
from dotenv import load_dotenv

from .api import BY_ID, Cloud, CloudError
from .storage import Store


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["add-project"])
    parser.add_argument("--owner", type=int, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    load_dotenv()
    allowed = {int(x.strip()) for x in os.environ["ALLOWED_USER_IDS"].split(",")}
    if args.owner not in allowed:
        raise SystemExit("Owner must be in ALLOWED_USER_IDS")
    token = getpass.getpass("Hetzner project API token (hidden): ").strip()
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        try:
            await Cloud(client).execute(token, BY_ID["list_servers"], {"query": {"per_page": 1}})
        except CloudError as error:
            raise SystemExit(str(error)) from None
    store = Store(os.environ.get("DATA_DIR", "./data"), os.environ["ENCRYPTION_KEY"])
    try:
        await asyncio.to_thread(store.add, args.owner, args.name, token)
    except sqlite3.IntegrityError:
        raise SystemExit("This project name already exists for the owner") from None
    print("Project added. Open /projects in Telegram.")


if __name__ == "__main__":
    asyncio.run(main())
