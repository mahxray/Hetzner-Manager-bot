import asyncio
from contextlib import suppress
import io
import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from .api import BY_ID, OPERATIONS, Cloud, CloudError, prepare, redact, sample, schema_for
from .storage import Store

TAGS = sorted({op["tag"] for op in OPERATIONS})
LABELS = {
    "Servers": "سرورها", "Server Actions": "عملیات سرور", "Images": "ایمیج و اسنپ‌شات",
    "Networks": "شبکه‌ها", "Firewalls": "فایروال‌ها", "Volumes": "دیسک‌ها",
    "SSH Keys": "کلیدهای SSH", "Floating IPs": "آی‌پی شناور", "Primary IPs": "آی‌پی اصلی",
    "Load Balancers": "لودبالانسرها", "Zones": "دامنه‌های DNS", "Zone RRSets": "رکوردهای DNS",
    "Certificates": "گواهی‌ها", "Placement Groups": "گروه‌های استقرار", "Pricing": "قیمت‌ها",
    "Locations": "موقعیت‌ها", "Server Types": "مدل سرورها",
}


def keyboard(rows):
    return Keyboard([[Button(label, callback_data=data) for label, data in row] for row in rows])


@dataclass(frozen=True)
class Pending:
    nonce: str
    project: str
    operation: str
    payload: dict
    expires: float


def consume_pending(state, nonce, now=None):
    pending = state.get("pending")
    if not pending or not secrets.compare_digest(pending.nonce, nonce):
        raise ValueError("این تأیید معتبر نیست یا قبلاً استفاده شده است.")
    state.pop("pending")
    if pending.expires < (time.monotonic() if now is None else now):
        raise ValueError("مهلت تأیید تمام شده؛ عملیات را دوباره انتخاب کنید.")
    return pending


def expire_state(state, now):
    pending = state.get("pending")
    if pending and pending.expires < now:
        state.pop("pending", None)
    for key in ("input", "receipt"):
        value = state.get(key)
        if value and value.get("expires", float("inf")) < now:
            state.pop(key, None)


class Manager:
    def __init__(self, store, cloud, allowed):
        self.store, self.cloud, self.allowed = store, cloud, allowed

    def authorized(self, update):
        return (update.effective_user is not None and update.effective_chat is not None
                and update.effective_chat.type == "private"
                and update.effective_user.id in self.allowed)

    async def project(self, update, pid):
        return await asyncio.to_thread(self.store.get, update.effective_user.id, pid)

    async def send_json(self, update, value, filename="result.json"):
        text = json.dumps(value, ensure_ascii=False, indent=2)
        if len(text.encode("utf-16-le")) // 2 <= 3500:
            await update.effective_message.reply_text(text)
        else:
            stream = io.BytesIO(text.encode())
            await update.effective_message.reply_document(stream, filename=filename)

    async def projects(self, update, context):
        if not self.authorized(update):
            return
        context.user_data.clear()
        projects = await asyncio.to_thread(self.store.list, update.effective_user.id)
        rows = [[(name, f"p:{pid}")] for pid, name in projects]
        rows.append([("➕ افزودن پروژه", "add")])
        await update.effective_message.reply_text(
            "پروژهٔ هتزنر را انتخاب کنید. هر پروژه توکن مستقل دارد.", reply_markup=keyboard(rows))

    async def help(self, update, context):
        if not self.authorized(update):
            return
        await update.effective_message.reply_text(
            "/projects — پروژه‌ها\n/add — افزودن پروژه\n/cancel — لغو ورودی و تأیید\n"
            "در پروژه: سرورها برای مدیریت سریع؛ تمام عملیات برای امکانات پیشرفتهٔ API.\n"
            "ورودی پیشرفته JSON است؛ نمونه و ساختار هر عملیات قابل دریافت است.\n"
            "هر تغییر نیازمند تأیید ۲ دقیقه‌ای است. پاسخ running به معنی پایان عملیات نیست؛ "
            "دکمهٔ پیگیری را بزنید. پاسخ محرمانه فقط با درخواست جدا نمایش داده می‌شود.")

    async def add(self, update, context):
        if not self.authorized(update):
            return
        context.user_data.clear()
        context.user_data["input"] = {"kind": "name"}
        await update.effective_message.reply_text("نام دلخواه پروژه را بفرستید؛ /cancel برای لغو.")

    async def menu(self, update, pid):
        name, _ = await self.project(update, pid)
        await update.effective_message.reply_text(f"پروژه: {name}", reply_markup=keyboard([
            [("🖥 سرورها", f"servers:{pid}:1"), ("➕ ساخت سرور", f"op:{pid}:{OPERATIONS.index(BY_ID['create_server'])}")],
            [("🧰 تمام عملیات Cloud", f"tags:{pid}:0")],
            [("حذف اتصال پروژه از بات", f"forget:{pid}")],
            [("پروژه‌ها", "projects")],
        ]))

    async def ask_operation(self, update, context, pid, index):
        op = OPERATIONS[index]
        name, _ = await self.project(update, pid)
        context.user_data.clear()
        payload = sample(schema_for(op))
        if any(p["in"] == "query" for p in op["parameters"]):
            payload.setdefault("query", {})
        if not op["parameters"] and not op["requestBody"] and op["method"] == "GET":
            await self.execute(update, context, pid, op, {})
            return
        context.user_data["input"] = {"kind": "operation", "pid": pid, "index": index,
                                      "expires": time.monotonic() + 600}
        note = "\n⚠ عملیات منسوخ‌شده در API." if op.get("deprecated") else ""
        await update.effective_message.reply_text(
            f"پروژه: {name}\n{op['summary']}\n{op['method']} {op['path']}{note}\n"
            "نمونهٔ زیر را با مقادیر خودتان ویرایش و به صورت متن یا فایل JSON ارسال کنید. "
            "نمونه ممکن است شامل شناسهٔ فرضی باشد. query اختیاری برای فیلتر و صفحه‌بندی است.\n/cancel",
            reply_markup=keyboard([[("راهنمای فیلدها (JSON Schema)", f"schema:{pid}:{index}")]]))
        await self.send_json(update, payload, "example.json")

    async def preview(self, update, context, pid, op, payload):
        prepare(op, payload)
        name, token = await self.project(update, pid)
        context.user_data.clear()
        pending = Pending(secrets.token_hex(12), pid, op["id"], payload, time.monotonic() + 120)
        context.user_data["pending"] = pending
        await update.effective_message.reply_text(
            f"⚠ تأیید تغییر — پروژه: {name}\n{op['summary']}\n{op['method']} {op['path']}\n"
            "این عملیات ممکن است هزینه ایجاد کند، سرویس را قطع کند یا داده را حذف کند. "
            "منبع و مقادیر را بررسی کنید. مهلت: ۲ دقیقه.")
        await self.send_json(update, redact(payload, (token,)))
        await update.effective_message.reply_text("اجرا شود؟", reply_markup=keyboard([
            [("✅ تأیید و اجرا", f"yes:{pending.nonce}"), ("لغو", "cancel")]]))

    async def execute(self, update, context, pid, op, payload):
        name, token = await self.project(update, pid)
        result = await self.cloud.execute(token, op, payload)
        logging.info("cloud_request project=%s operation=%s", pid, op["id"])
        await update.effective_message.reply_text(f"پروژه: {name} — پاسخ هتزنر:")
        await self.send_json(update, redact(result, (token,)))
        # Sensitive responses are private and opt-in, never logs or persistent storage.
        if redact(result, (token,)) != redact(result, (token,), hide_fields=False):
            nonce = secrets.token_hex(12)
            context.user_data["receipt"] = {
                "nonce": nonce, "expires": time.monotonic() + 120,
                "value": redact(result, (token,), hide_fields=False), "project": pid,
            }
            await update.effective_message.reply_text(
                "پاسخ شامل اطلاعات محرمانه مثل گذرواژه یا کنسول است. برای دریافت پاسخ کامل در همین "
                "چت خصوصی، دکمه را بزنید و سپس پیام را دستی حذف کنید. مهلت: ۲ دقیقه.",
                reply_markup=keyboard([[("دریافت پاسخ کامل محرمانه", f"reveal:{nonce}")]]))
        actions = result.get("actions", [])
        if isinstance(result.get("action"), dict):
            actions = [result["action"]] + actions
        rows = [[(f"پیگیری عملیات {action['id']}", f"action:{pid}:{action['id']}")]
                for action in actions[:10] if isinstance(action, dict) and "id" in action]
        rows.append([("بازگشت به پروژه", f"p:{pid}")])
        await update.effective_message.reply_text("ادامه:", reply_markup=keyboard(rows))

    async def servers(self, update, pid, page):
        name, token = await self.project(update, pid)
        result = await self.cloud.execute(token, BY_ID["list_servers"],
                                          {"query": {"page": page, "per_page": 10}})
        rows = [[(f"{server['name']} · {server['status']}", f"srv:{pid}:{server['id']}")]
                for server in result["servers"]]
        nav = []
        if page > 1:
            nav.append(("قبلی", f"servers:{pid}:{page-1}"))
        if result.get("meta", {}).get("pagination", {}).get("next_page"):
            nav.append(("بعدی", f"servers:{pid}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([("بازگشت", f"p:{pid}")])
        await update.effective_message.reply_text(f"سرورهای پروژه: {name} — صفحه {page}",
                                                  reply_markup=keyboard(rows))

    async def callback(self, update, context):
        if not self.authorized(update):
            if update.callback_query:
                await update.callback_query.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        await update.callback_query.answer()
        data = update.callback_query.data.split(":")
        kind = data[0]
        if kind in {"projects", "cancel"}:
            await self.projects(update, context)
            return
        if kind == "add":
            await self.add(update, context)
            return
        if kind == "reveal":
            receipt = context.user_data.get("receipt")
            if not receipt or not secrets.compare_digest(receipt["nonce"], data[1]):
                raise ValueError("این پاسخ دیگر در دسترس نیست.")
            context.user_data.pop("receipt")
            if receipt["expires"] < time.monotonic():
                raise ValueError("مهلت دریافت پاسخ محرمانه تمام شده است.")
            name, _ = await self.project(update, receipt["project"])
            await update.effective_message.reply_text(f"⚠ پاسخ کامل محرمانه — پروژه: {name}")
            await self.send_json(update, receipt["value"], "sensitive-result.json")
            return
        if kind == "yes":
            pending = consume_pending(context.user_data, data[1])
            if pending.operation == "forget":
                await asyncio.to_thread(self.store.remove, update.effective_user.id, pending.project)
                await self.projects(update, context)
            else:
                await self.execute(update, context, pending.project, BY_ID[pending.operation], pending.payload)
            return
        pid = data[1]
        name, token = await self.project(update, pid)
        if kind not in {"schema"}:
            context.user_data.clear()
        if kind == "p":
            await self.menu(update, pid)
        elif kind == "forget":
            nonce = secrets.token_hex(12)
            context.user_data["pending"] = Pending(nonce, pid, "forget", {}, time.monotonic() + 120)
            await update.effective_message.reply_text(
                f"اتصال {name} از بات حذف شود؟ منابع هتزنر حذف نمی‌شوند؛ توکن در هتزنر باطل نمی‌شود.",
                reply_markup=keyboard([[("حذف اتصال", f"yes:{nonce}"), ("لغو", "cancel")]]))
        elif kind == "servers":
            await self.servers(update, pid, int(data[2]))
        elif kind == "srv":
            server = (await self.cloud.execute(token, BY_ID["get_server"],
                                               {"path": {"id": int(data[2])}}))["server"]
            ip = server.get("public_net", {}).get("ipv4") or {}
            await update.effective_message.reply_text(
                f"پروژه: {name}\n{server['name']} — {server['id']}\n"
                f"وضعیت: {server['status']}\nIPv4: {ip.get('ip', '—')}",
                reply_markup=keyboard([
                    [(label, f"quick:{pid}:{data[2]}:{action}") for label, action in pair]
                    for pair in [[("روشن", "on"), ("خاموش نرم", "stop")],
                                 [("ری‌استارت", "reboot"), ("قطع برق", "off")],
                                 [("اسنپ‌شات", "snap"), ("حذف سرور", "del")]]
                    ] + [[("بازگشت", f"servers:{pid}:1")]]))
        elif kind == "quick":
            operation, body = {
                "on": ("poweron_server", None), "stop": ("shutdown_server", None),
                "reboot": ("reboot_server", None), "off": ("poweroff_server", None),
                "snap": ("create_server_image", {"type": "snapshot"}),
                "del": ("delete_server", None),
            }[data[3]]
            payload = {"path": {"id": int(data[2])}}
            if body is not None:
                payload["body"] = body
            await self.preview(update, context, pid, BY_ID[operation], payload)
        elif kind == "action":
            await self.execute(update, context, pid, BY_ID["get_action"], {"path": {"id": int(data[2])}})
        elif kind == "tags":
            page = int(data[2])
            rows = [[(LABELS.get(tag, tag), f"tag:{pid}:{i}:0")]
                    for i, tag in enumerate(TAGS) if page * 10 <= i < (page + 1) * 10]
            nav = []
            if page:
                nav.append(("قبلی", f"tags:{pid}:{page-1}"))
            if (page + 1) * 10 < len(TAGS):
                nav.append(("بعدی", f"tags:{pid}:{page+1}"))
            if nav:
                rows.append(nav)
            rows.append([("بازگشت", f"p:{pid}")])
            await update.effective_message.reply_text(f"پروژه: {name} — گروه عملیات", reply_markup=keyboard(rows))
        elif kind == "tag":
            tag_index, page = int(data[2]), int(data[3])
            ops = [(i, op) for i, op in enumerate(OPERATIONS) if op["tag"] == TAGS[tag_index]]
            rows = [[(f"{op['method']} · {op['summary']}", f"op:{pid}:{i}")]
                    for i, op in ops[page*8:(page+1)*8]]
            nav = []
            if page:
                nav.append(("قبلی", f"tag:{pid}:{tag_index}:{page-1}"))
            if (page+1)*8 < len(ops):
                nav.append(("بعدی", f"tag:{pid}:{tag_index}:{page+1}"))
            if nav:
                rows.append(nav)
            rows.append([("گروه‌ها", f"tags:{pid}:0")])
            await update.effective_message.reply_text(f"پروژه: {name} — {TAGS[tag_index]}", reply_markup=keyboard(rows))
        elif kind == "schema":
            await self.send_json(update, schema_for(OPERATIONS[int(data[2])]), "input-schema.json")
        elif kind == "op":
            await self.ask_operation(update, context, pid, int(data[2]))

    async def text(self, update, context, supplied_text=None):
        if not self.authorized(update):
            return
        state = context.user_data.get("input")
        if not state:
            await update.effective_message.reply_text("از /projects شروع کنید.")
            return
        text = supplied_text if supplied_text is not None else update.effective_message.text.strip()
        if state["kind"] == "name":
            if not text or len(text) > 60:
                raise ValueError("نام پروژه باید بین ۱ تا ۶۰ حرف باشد.")
            context.user_data["input"] = {"kind": "token", "name": text,
                                          "expires": time.monotonic() + 600}
            await update.effective_message.reply_text(
                "توکن API این پروژه را بفرستید. پیام حاوی توکن را حذف می‌کنم و توکن رمزگذاری می‌شود. "
                "چت بات تلگرام رمزگذاری سرتاسری ندارد؛ برای وارد کردن توکن بدون تلگرام از "
                "دستور add-project در راهنما استفاده کنید. /cancel")
        elif state["kind"] == "token":
            try:
                await update.effective_message.delete()
            except TelegramError:
                await update.effective_message.reply_text("حذف پیام ممکن نشد؛ پیام توکن را دستی حذف کنید.")
            if state["expires"] < time.monotonic():
                context.user_data.clear()
                raise ValueError("مهلت ورود توکن تمام شد؛ /add")
            if not text.isascii() or not text.isalnum() or not 32 <= len(text) <= 256:
                raise ValueError("قالب توکن نامعتبر است؛ توکن را دوباره بفرستید.")
            await self.cloud.execute(text, BY_ID["list_servers"], {"query": {"per_page": 1}})
            try:
                pid = await asyncio.to_thread(self.store.add, update.effective_user.id, state["name"], text)
            except sqlite3.IntegrityError:
                raise ValueError("این نام پروژه قبلاً ثبت شده؛ با /add نام دیگری انتخاب کنید.") from None
            context.user_data.clear()
            await self.menu(update, pid)
        else:
            # JSON bodies may include passwords or SSH material. Remove the original message too.
            try:
                await update.effective_message.delete()
            except TelegramError:
                await update.effective_message.reply_text("اگر ورودی محرمانه است، پیام را دستی حذف کنید.")
            if state["expires"] < time.monotonic():
                context.user_data.clear()
                raise ValueError("مهلت ورودی تمام شده؛ عملیات را دوباره انتخاب کنید.")
            try:
                payload = json.loads(text)
            except ValueError:
                raise ValueError("JSON معتبر نیست؛ از کوتیشن انگلیسی استفاده کنید.") from None
            op = OPERATIONS[state["index"]]
            prepare(op, payload)
            if op["method"] == "GET":
                context.user_data.clear()
                await self.execute(update, context, state["pid"], op, payload)
            else:
                await self.preview(update, context, state["pid"], op, payload)

    async def document(self, update, context):
        if not self.authorized(update):
            return
        state = context.user_data.get("input", {})
        if state.get("kind") != "operation":
            raise ValueError("اول عملیات مورد نظر را انتخاب کنید؛ سپس فایل JSON بفرستید.")
        document = update.effective_message.document
        if not document.file_size or document.file_size > 512 * 1024:
            raise ValueError("حداکثر حجم فایل JSON برابر ۵۱۲ کیلوبایت است.")
        file = await document.get_file()
        raw = await file.download_as_bytearray()
        if len(raw) > 512 * 1024:
            raise ValueError("فایل بیش از حد بزرگ است.")
        try:
            value = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("فایل باید JSON با کدگذاری UTF-8 باشد.") from None
        await self.text(update, context, supplied_text=value)

    async def error(self, update, context):
        # Never log update objects, exception messages, HTTP URLs or tracebacks: tokens may be present.
        logging.error("handler_failure type=%s", type(context.error).__name__)
        if isinstance(update, Update) and self.authorized(update):
            message = (str(context.error) if isinstance(context.error, (ValueError, CloudError))
                       else "خطای داخلی؛ عملیات خودکار تکرار نشد. وضعیت منبع را بررسی کنید.")
            # ValueErrors from third-party libraries may include input; only our handlers produce
            # expected ValueErrors in normal operation. Other failures stay generic.
            await update.effective_message.reply_text(message[:3000])


def main():
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed = {int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
    if not token or token.startswith("replace_") or not allowed or min(allowed) <= 0:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and positive ALLOWED_USER_IDS in .env")
    store = Store(os.environ.get("DATA_DIR", "./data"), os.environ["ENCRYPTION_KEY"])
    client = httpx.AsyncClient(timeout=30, follow_redirects=False)
    manager = Manager(store, Cloud(client), allowed)

    async def cleanup(application):
        while True:
            for state in list(application.user_data.values()):
                expire_state(state, time.monotonic())
            await asyncio.sleep(15)

    async def startup(application):
        application.bot_data["cleanup"] = asyncio.create_task(cleanup(application), name="expire-private-state")

    async def shutdown(application):
        task = application.bot_data.get("cleanup")
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await client.aclose()

    application = (Application.builder().token(token).concurrent_updates(False)
                   .post_init(startup).post_shutdown(shutdown).build())
    application.add_handler(CommandHandler(["start", "projects", "cancel"], manager.projects))
    application.add_handler(CommandHandler("add", manager.add))
    application.add_handler(CommandHandler("help", manager.help))
    application.add_handler(CallbackQueryHandler(manager.callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manager.text))
    application.add_handler(MessageHandler(filters.Document.ALL, manager.document))
    application.add_error_handler(manager.error)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("telegram").setLevel(logging.CRITICAL)
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
