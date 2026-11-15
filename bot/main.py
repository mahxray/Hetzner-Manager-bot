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
from .forms import Forms, title
from .simple import SimpleUI, STATUS, server_card

TAGS = sorted({op["tag"] for op in OPERATIONS})
COLLECTIONS = [
    ("servers", "سرورها"), ("images", "بکاپ‌ها، اسنپ‌شات‌ها و ایمیج‌ها"),
    ("networks", "شبکه‌ها"), ("firewalls", "فایروال‌ها"), ("volumes", "دیسک‌ها"),
    ("floating_ips", "Floating IP"), ("primary_ips", "Primary IP"),
    ("ssh_keys", "کلیدهای SSH"), ("load_balancers", "لودبالانسرها"),
    ("certificates", "گواهی‌ها"), ("placement_groups", "گروه‌های استقرار"), ("zones", "دامنه‌های DNS"),
]
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
    simple: bool = False


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
    keyboard = staticmethod(keyboard)

    def __init__(self, store, cloud, allowed):
        self.store, self.cloud, self.allowed = store, cloud, allowed
        self.forms = Forms(self)
        self.simple = SimpleUI(self)

    async def show(self, update, text, rows):
        message = update.effective_message
        if getattr(update.callback_query, 'data', None) and hasattr(message, 'edit_text'):
            try:
                await message.edit_text(text, reply_markup=keyboard(rows))
                return
            except TelegramError:
                pass
        await message.reply_text(text, reply_markup=keyboard(rows))

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
            "۱. پروژه‌ات را انتخاب کن.\n۲. وارد سرورها، اسنپ‌شات‌ها یا IPها شو.\n"
            "۳. منبع و کار مورد نظر را انتخاب کن؛ بات مرحله‌به‌مرحله راهنمایی می‌کند.\n"
            "ساخت سرور: نام ← کشور ← پلن موجود ← سیستم‌عامل ← کلید SSH ← تأیید.\n"
            "ابزارهای تخصصی در تنظیمات پروژه ← بخش پیشرفته هستند.\n"
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
        await self.show(update, f"📁 {name}\nچه کاری می‌خواهی انجام بدهی؟", [
            [("🖥 سرورهای من", f"servers:{pid}:1"), ("➕ ساخت سرور", f"new:{pid}:{OPERATIONS.index(BY_ID['create_server'])}")],
            [("📸 اسنپ‌شات و بکاپ", f"images:{pid}:snapshot:1"), ("🌐 آدرس‌های IP", f"ips:{pid}:1")],
            [("⚙️ تنظیمات پروژه", f"settings:{pid}")],
            [("↩️ تغییر پروژه", "projects")],
        ])

    async def simple_screen(self, update, context, pid, kind, args):
        project, token = await self.project(update, pid)
        def action(label, operation, resource_id=None):
            index = OPERATIONS.index(BY_ID[operation])
            return (label, f'do:{pid}:{index}:{resource_id}' if resource_id is not None else f'new:{pid}:{index}')
        if kind == 'sub':
            server_id, section = int(args[0]), args[1]
            server = (await self.cloud.execute(token, BY_ID['get_server'], {'path': {'id': server_id}}))['server']
            if section == 'backup':
                rows = [[action('📸 ساخت اسنپ‌شات', 'create_server_image', server_id)],
                        [action('فعال کردن بکاپ خودکار', 'enable_server_backup', server_id),
                         action('خاموش کردن بکاپ خودکار', 'disable_server_backup', server_id)],
                        [('📂 اسنپ‌شات‌ها', f'images:{pid}:snapshot:1'), ('📂 بکاپ‌ها', f'images:{pid}:backup:1')]]
            else:
                rows = [[action('✏️ تغییر نام', 'update_server', server_id), action('⬆️ تغییر پلن', 'change_server_type', server_id)],
                        [action('🛠 نصب مجدد / بازیابی', 'rebuild_server', server_id)],
                        [action('🔑 بازنشانی گذرواژه', 'reset_server_password', server_id)],
                        [action('فعال کردن Rescue', 'enable_server_rescue', server_id),
                         action('غیرفعال کردن Rescue', 'disable_server_rescue', server_id)],
                        [action('قطع برق اجباری', 'poweroff_server', server_id), action('🗑 حذف سرور', 'delete_server', server_id)],
                        [('تنظیمات تخصصی', f'item:{pid}:0:{server_id}:0')]]
            rows.append([('بازگشت به سرور', f'srv:{pid}:{server_id}')])
            await self.show(update, f"{server['name']}\nپروژه: {project}\n{'اسنپ‌شات و بکاپ' if section == 'backup' else 'تغییرات سرور'}", rows)
            return
        if kind in {'images', 'ips'}:
            page = int(args[-1])
            collection = 'images' if kind == 'images' else 'primary_ips'
            query = {'page': page, 'per_page': 8}
            if kind == 'images':
                query['type'] = [args[0]]
            result = await self.cloud.execute(token, BY_ID['list_' + collection], {'query': query})
            rows = []
            for item in result[collection]:
                label = item.get('name') or item.get('description') or str(item['id'])
                if kind == 'ips':
                    label += f" · {item.get('ip', 'در حال تخصیص')}"
                rows.append([(label, f"{'pic' if kind == 'images' else 'ip'}:{pid}:{item['id']}")])
            nav = []
            prefix = f'images:{pid}:{args[0]}' if kind == 'images' else f'ips:{pid}'
            if page > 1:
                nav.append(('قبلی', f'{prefix}:{page-1}'))
            if result.get('meta', {}).get('pagination', {}).get('next_page'):
                nav.append(('بعدی', f'{prefix}:{page+1}'))
            if nav:
                rows.append(nav)
            if kind == 'images':
                rows += [[('📸 اسنپ‌شات‌ها', f'images:{pid}:snapshot:1'), ('بکاپ‌های خودکار', f'images:{pid}:backup:1')],
                         [action('➕ ساخت اسنپ‌شات', 'create_server_image')]]
                heading = 'اسنپ‌شات‌ها' if args[0] == 'snapshot' else 'بکاپ‌های خودکار'
            else:
                rows.append([action('➕ ساخت IP', 'create_primary_ip')])
                heading = 'آدرس‌های IP'
            rows.append([('🔄 تازه‌سازی', f'{prefix}:{page}'), ('بازگشت', f'p:{pid}')])
            await self.show(update, f"{heading}\nپروژه: {project}\n" +
                            ('یک مورد را انتخاب کن.' if result[collection] else 'هنوز موردی وجود ندارد.'), rows)
            return
        resource_id = int(args[0])
        if kind == 'pic':
            item = (await self.cloud.execute(token, BY_ID['get_image'], {'path': {'id': resource_id}}))['image']
            await self.show(update, f"📸 {item.get('description') or item.get('name') or resource_id}\n"
                            f"پروژه: {project}\nاندازه: {item.get('image_size', '—')} GB\n"
                            f"وضعیت: {item.get('status', '—')}", [
                [('♻️ بازیابی روی سرور', f'restore:{pid}:{resource_id}')],
                [action('✏️ تغییر نام', 'update_image', resource_id), action('🗑 حذف', 'delete_image', resource_id)],
                [('بازگشت', f'images:{pid}:snapshot:1')]])
        elif kind == 'ip':
            item = (await self.cloud.execute(token, BY_ID['get_primary_ip'], {'path': {'id': resource_id}}))['primary_ip']
            await self.show(update, f"🌐 {item['name']}\nپروژه: {project}\nIP: {item.get('ip', '—')}\n"
                            f"سرور متصل: {item.get('assignee_id') or 'متصل نیست'}", [
                [action('اتصال به سرور', 'assign_primary_ip', resource_id), action('جدا کردن IP', 'unassign_primary_ip', resource_id)],
                [action('✏️ تغییر نام', 'update_primary_ip', resource_id), action('🗑 حذف IP', 'delete_primary_ip', resource_id)],
                [('بازگشت', f'ips:{pid}:1')]])

    async def resources(self, update, pid, collection_index, page):
        name, token = await self.project(update, pid)
        collection, label = COLLECTIONS[collection_index]
        op = next(o for o in OPERATIONS if o['path'] == '/' + collection and o['method'] == 'GET')
        result = await self.cloud.execute(token, op, {"query": {"page": page, "per_page": 8}})
        rows = [[(f"{resource.get('name') or resource.get('description') or resource.get('ip') or resource['id']} · {resource['id']}",
                  f"item:{pid}:{collection_index}:{resource['id']}:0")]
                for resource in result[collection]]
        nav = []
        if page > 1:
            nav.append(("قبلی", f"res:{pid}:{collection_index}:{page-1}"))
        if result.get('meta', {}).get('pagination', {}).get('next_page'):
            nav.append(("بعدی", f"res:{pid}:{collection_index}:{page+1}"))
        if nav:
            rows.append(nav)
        create = next((i for i, o in enumerate(OPERATIONS)
                       if o['path'] == '/' + collection and o['method'] == 'POST'), None)
        if create is not None:
            rows.append([("➕ ساخت / افزودن", f"form:{pid}:{create}")])
        rows.append([("منابع", f"resources:{pid}")])
        await update.effective_message.reply_text(f"پروژه: {name}\n{label} — صفحه {page}", reply_markup=keyboard(rows))

    async def resource_item(self, update, pid, collection_index, resource_id, page):
        name, token = await self.project(update, pid)
        collection, label = COLLECTIONS[collection_index]
        base = '/' + collection + '/'
        operations = [(i, o) for i, o in enumerate(OPERATIONS)
                      if o['path'].startswith(base + '{') and not o['path'].startswith(base + 'actions/')]
        get = next(o for _, o in operations if o['method'] == 'GET' and o['path'].count('/') == 2)
        key = next(p['name'] for p in get['parameters'] if p['in'] == 'path')
        parameter = resource_id if key == 'id_or_name' else int(resource_id)
        if page == 0:
            result = await self.cloud.execute(token, get, {"path": {key: parameter}})
            await self.send_json(update, redact(result, (token,)))
        # Include all operations on the selected resource, including DNS RRSet subresources.
        rows = [[(title(o), f"edit:{pid}:{i}:{resource_id}")]
                for i, o in operations[page*8:(page+1)*8]]
        nav = []
        if page:
            nav.append(("قبلی", f"item:{pid}:{collection_index}:{resource_id}:{page-1}"))
        if (page+1)*8 < len(operations):
            nav.append(("بعدی", f"item:{pid}:{collection_index}:{resource_id}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([("بازگشت به فهرست", f"res:{pid}:{collection_index}:1")])
        await update.effective_message.reply_text(f"پروژه: {name}\n{label} — شناسه {resource_id}\nعملیات:",
                                                  reply_markup=keyboard(rows))

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

    async def preview(self, update, context, pid, op, payload, simple=False, labels=None):
        prepare(op, payload)
        name, token = await self.project(update, pid)
        context.user_data.clear()
        pending = Pending(secrets.token_hex(12), pid, op["id"], payload, time.monotonic() + 120, simple)
        context.user_data["pending"] = pending
        if simple:
            from .forms import FIELDS
            details = dict(labels or {})
            for key, value in payload.get('body', {}).items():
                if key not in details:
                    details[key] = 'بله' if value is True else 'خیر' if value is False else str(value)
            if payload.get('path') and 'سرور' not in details:
                details['شناسهٔ منبع'] = str(next(iter(payload['path'].values())))
            warning = ('⚠️ تمام اطلاعات دیسک پاک می‌شود.' if op['id'] == 'rebuild_server' else
                       '⚠️ حذف منبع دائمی است.' if op['method'] == 'DELETE' else
                       'ایجاد یا تغییر منابع می‌تواند هزینه داشته باشد و اتصال سرور را قطع کند.')
            text = f"{title(op)}\nپروژه: {name}\n\n" + '\n'.join(
                f"{FIELDS.get(k, k)}: {str(v)[:250]}" for k, v in details.items())
            await self.show(update, text[:2800] + f"\n\n{warning}\nانجام شود؟", [
                [('✅ تأیید و انجام', f'yes:{pending.nonce}'), ('✖️ انصراف', f'p:{pid}')]])
            return
        await update.effective_message.reply_text(
            f"⚠ تأیید تغییر — پروژه: {name}\n{op['summary']}\n{op['method']} {op['path']}\n"
            "این عملیات ممکن است هزینه ایجاد کند، سرویس را قطع کند یا داده را حذف کند. "
            "منبع و مقادیر را بررسی کنید. مهلت: ۲ دقیقه.")
        await self.send_json(update, redact(payload, (token,)))
        await update.effective_message.reply_text("اجرا شود؟", reply_markup=keyboard([
            [("✅ تأیید و اجرا", f"yes:{pending.nonce}"), ("لغو", "cancel")]]))

    async def execute(self, update, context, pid, op, payload, simple=False):
        name, token = await self.project(update, pid)
        if simple and op['id'] == 'create_server':
            await self.simple.validate_create(token, payload)
        result = await self.cloud.execute(token, op, payload)
        logging.info("cloud_request project=%s operation=%s", pid, op["id"])
        if simple:
            action = result.get('action', {})
            status = action.get('status')
            message = ('⏳ درخواست ثبت شد و در حال انجام است.' if status == 'running' else
                       '❌ عملیات در هتزنر ناموفق بود.' if status == 'error' else
                       '✅ عملیات انجام شد.' if status == 'success' else '✅ هتزنر درخواست را پذیرفت.')
            if isinstance(result.get('server'), dict) and 'name' in result['server']:
                message += f"\nسرور: {result['server']['name']}\nIPv4: {((result['server'].get('public_net') or {}).get('ipv4') or {}).get('ip', 'هنوز تخصیص داده نشده')}"
        else:
            await update.effective_message.reply_text(f"پروژه: {name} — پاسخ هتزنر:")
            await self.send_json(update, redact(result, (token,)))
        # Sensitive responses are private and opt-in, never logs or persistent storage.
        full_result = redact(result, (token,), hide_fields=False)
        receipt_value = full_result
        if simple:
            receipt_value = {k: v for k, v in full_result.items()
                             if k in {'root_password', 'password', 'wss_url', 'ticket'} and v}
        if (simple and receipt_value) or (not simple and redact(result, (token,)) != full_result):
            nonce = secrets.token_hex(12)
            context.user_data["receipt"] = {
                "nonce": nonce, "expires": time.monotonic() + 120,
                "value": receipt_value, "project": pid, 'simple': simple,
            }
            await update.effective_message.reply_text(
                "پاسخ شامل اطلاعات محرمانه مثل گذرواژه یا کنسول است. برای دریافت پاسخ کامل در همین "
                "چت خصوصی، دکمه را بزنید و سپس پیام را دستی حذف کنید. مهلت: ۲ دقیقه.",
                reply_markup=keyboard([[("دریافت پاسخ کامل محرمانه", f"reveal:{nonce}")]]))
        actions = result.get("actions", [])
        if isinstance(result.get("action"), dict):
            actions = [result["action"]] + actions
        rows = [[(f"پیگیری عملیات {action['id']}", f"{'check' if simple else 'action'}:{pid}:{action['id']}")]
                for action in actions[:10] if isinstance(action, dict) and "id" in action]
        rows.append([("بازگشت به پروژه", f"p:{pid}")])
        if simple:
            server_id = (result.get('server') or {}).get('id')
            if not server_id and op['path'].startswith('/servers/') and op['method'] != 'DELETE':
                server_id = payload.get('path', {}).get('id')
            if server_id:
                rows.insert(0, [('🖥 نمایش سرور', f'srv:{pid}:{server_id}')])
        if simple:
            await self.show(update, f"پروژه: {name}\n{message}", rows)
        else:
            await update.effective_message.reply_text("ادامه:", reply_markup=keyboard(rows))

    async def servers(self, update, pid, page):
        name, token = await self.project(update, pid)
        result = await self.cloud.execute(token, BY_ID["list_servers"],
                                          {"query": {"page": page, "per_page": 10}})
        rows = [[(f"{STATUS.get(server['status'], server['status'])} · {server['name']}", f"srv:{pid}:{server['id']}")]
                for server in result["servers"] if server.get('status') != 'deleting']
        nav = []
        if page > 1:
            nav.append(("قبلی", f"servers:{pid}:{page-1}"))
        if result.get("meta", {}).get("pagination", {}).get("next_page"):
            nav.append(("بعدی", f"servers:{pid}:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([('➕ ساخت سرور', f"new:{pid}:{OPERATIONS.index(BY_ID['create_server'])}")])
        rows.append([('🔄 تازه‌سازی', f'servers:{pid}:{page}'), ("بازگشت", f"p:{pid}")])
        text = f"🖥 سرورهای {name}\nسرور مورد نظر را انتخاب کن."
        if not result['servers']:
            text = f"🖥 {name}\nهنوز سروری در این پروژه نیست. از «ساخت سرور» شروع کن."
        await self.show(update, text, rows)

    async def callback(self, update, context):
        if not self.authorized(update):
            if update.callback_query:
                await update.callback_query.answer("دسترسی مجاز نیست.", show_alert=True)
            return
        await update.callback_query.answer()
        data = update.callback_query.data.split(":")
        kind = data[0]
        if kind == 'easy':
            await self.simple.callback(update, context, data)
            return
        if kind == "flow":
            await self.forms.callback(update, context, data)
            return
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
            if receipt.get('simple'):
                names = {'root_password': 'گذرواژهٔ سرور', 'password': 'گذرواژه',
                         'wss_url': 'آدرس کنسول', 'ticket': 'کلید اتصال کنسول'}
                await update.effective_message.reply_text('\n'.join(
                    f"{names.get(k, k)}: {v}" for k, v in receipt['value'].items()))
            else:
                await self.send_json(update, receipt["value"], "sensitive-result.json")
            return
        if kind == "yes":
            pending = consume_pending(context.user_data, data[1])
            if pending.operation == "forget":
                await asyncio.to_thread(self.store.remove, update.effective_user.id, pending.project)
                await self.projects(update, context)
            else:
                await self.execute(update, context, pending.project, BY_ID[pending.operation], pending.payload,
                                   simple=pending.simple)
            return
        pid = data[1]
        name, token = await self.project(update, pid)
        if kind not in {"schema"}:
            context.user_data.clear()
        if kind == "p":
            await self.menu(update, pid)
        elif kind in {'new', 'do'}:
            await self.simple.start(update, context, pid, OPERATIONS[int(data[2])]['id'],
                                    data[3] if kind == 'do' else None)
        elif kind == 'restore':
            await self.simple.start(update, context, pid, 'rebuild_server', image_id=int(data[2]))
        elif kind == 'settings':
            await self.show(update, f'⚙️ تنظیمات {name}', [
                [('🧰 بخش پیشرفته', f'advanced:{pid}')],
                [('حذف اتصال این پروژه از بات', f'forget:{pid}')], [('بازگشت', f'p:{pid}')]])
        elif kind == 'advanced':
            await self.show(update, f'🧰 ابزارهای پیشرفتهٔ {name}\nبرای کارهای روزمره از منوی اصلی استفاده کن.', [
                [('شبکه، فایروال، دیسک، DNS و سایر منابع', f'resources:{pid}')],
                [('تمام عملیات و ورودی تخصصی', f'tags:{pid}:0')], [('بازگشت', f'p:{pid}')]])
        elif kind in {'images', 'ips', 'pic', 'ip', 'sub'}:
            await self.simple_screen(update, context, pid, kind, data[2:])
        elif kind == "resources":
            rows = [[(label, f"res:{pid}:{i}:1")] for i, (_, label) in enumerate(COLLECTIONS)]
            rows.append([("بازگشت", f"p:{pid}")])
            await update.effective_message.reply_text(f"پروژه: {name} — منابع", reply_markup=keyboard(rows))
        elif kind == "res":
            await self.resources(update, pid, int(data[2]), int(data[3]))
        elif kind == "item":
            await self.resource_item(update, pid, int(data[2]), data[3], int(data[4]))
        elif kind in {"form", "edit"}:
            index = int(data[2])
            path = None
            if kind == "edit":
                key = OPERATIONS[index]['path'].split('/{', 1)[1].split('}', 1)[0]
                path = {key: data[3] if key == 'id_or_name' else int(data[3])}
            await self.forms.start(update, context, pid, index, path)
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
            def action(label, op):
                return (label, f"do:{pid}:{OPERATIONS.index(BY_ID[op])}:{server['id']}")
            await self.show(update, server_card(server, name), [
                [action('▶️ روشن کردن', 'poweron_server'), action('⏹ خاموش کردن', 'shutdown_server')],
                [action('🔄 ری‌استارت', 'reboot_server')],
                [('📸 اسنپ‌شات و بکاپ', f"sub:{pid}:{server['id']}:backup")],
                [('⚙️ تغییرات سرور', f"sub:{pid}:{server['id']}:settings")],
                [('🔄 تازه‌سازی', f"srv:{pid}:{server['id']}"), ('بازگشت', f'servers:{pid}:1')]])
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
        elif kind in {"action", "check"}:
            await self.execute(update, context, pid, BY_ID["get_action"], {"path": {"id": int(data[2])}}, simple=kind == 'check')
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
            rows = [[(title(op), f"form:{pid}:{i}"), ("JSON", f"op:{pid}:{i}")]
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
        if state['kind'] == 'simple':
            await self.simple.text(update, context, text)
            return
        if state["kind"] == "form":
            try:
                await update.effective_message.delete()
            except TelegramError:
                await update.effective_message.reply_text("اگر ورودی محرمانه است، پیام را دستی حذف کنید.")
            form_text = supplied_text if supplied_text is not None else update.effective_message.text
            await self.forms.text(update, context, form_text)
            return
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
        if state.get("kind") != "operation" and not (
                state.get("kind") == "form" and self.forms.accepts_document(context)):
            raise ValueError("اول عملیات JSON یا یک فیلد متنی فرم را انتخاب کنید؛ سپس فایل بفرستید.")
        document = update.effective_message.document
        if not document.file_size or document.file_size > 512 * 1024:
            raise ValueError("حداکثر حجم فایل ورودی برابر ۵۱۲ کیلوبایت است.")
        file = await document.get_file()
        raw = await file.download_as_bytearray()
        if len(raw) > 512 * 1024:
            raise ValueError("فایل بیش از حد بزرگ است.")
        try:
            value = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("فایل باید کدگذاری UTF-8 داشته باشد.") from None
        await self.text(update, context, supplied_text=value)

    async def error(self, update, context):
        # Never log update objects, exception messages, HTTP URLs or tracebacks: tokens may be present.
        logging.error("handler_failure type=%s", type(context.error).__name__)
        if isinstance(update, Update) and self.authorized(update):
            message = (str(context.error) if isinstance(context.error, (ValueError, CloudError))
                       else "خطای داخلی؛ عملیات خودکار تکرار نشد. وضعیت منبع را بررسی کنید.")
            if isinstance(context.error, CloudError):
                if 'not_found' in message:
                    message = 'این منبع دیگر در پروژه پیدا نشد. به فهرست برگرد و «تازه‌سازی» را بزن.'
                elif 'resource_unavailable' in message:
                    message = 'ظرفیت پلن انتخاب‌شده تمام شده است. دوباره از فهرست پلن‌های موجود انتخاب کن.'
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
