import asyncio
from contextlib import suppress
import io
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from .api import BY_ID, OPERATIONS, Cloud, CloudError, prepare, redact, sample, schema_for
from .storage import Store
from .forms import Forms, title
from .simple import SimpleUI, STATUS, server_card
from .ip_control import IPControl
from .availability import all_items
from .i18n import LANGUAGE, tr

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
    return Keyboard([[Button(label if data.startswith(('p:', 'pic:', 'ip:', 'item:')) else tr(label), callback_data=data)
                     for label, data in row] for row in rows])


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
        raise ValueError(tr('این تأیید معتبر نیست یا قبلاً استفاده شده است.'))
    state.pop("pending")
    if pending.expires < (time.monotonic() if now is None else now):
        raise ValueError(tr('مهلت تأیید تمام شده؛ عملیات را دوباره انتخاب کنید.'))
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
        self.ip_control = IPControl(store, cloud)
        self.languages = store.languages()

    async def show(self, update, text, rows):
        message = update.effective_message
        if getattr(update.callback_query, 'data', None) and hasattr(message, 'edit_text'):
            try:
                await message.edit_text(text, reply_markup=keyboard(rows))
                return
            except BadRequest as error:
                if 'message is not modified' in str(error).lower():
                    return
            except TelegramError:
                pass
        await message.reply_text(text, reply_markup=keyboard(rows))

    @staticmethod
    def reset(context):
        nav = context.user_data.get('nav')
        context.user_data.clear()
        if nav:
            context.user_data['nav'] = nav

    def authorized(self, update):
        allowed = (update.effective_user is not None and update.effective_chat is not None
                and update.effective_chat.type == "private"
                and update.effective_user.id in self.allowed)
        if allowed:
            LANGUAGE.set(self.languages.get(update.effective_user.id, 'fa'))
        return allowed

    async def language(self, update, context):
        if not self.authorized(update):
            return
        context.user_data.clear()
        await self.show(update, tr('زبان بات را انتخاب کنید. / Choose your language.'), [[(tr('فارسی / Persian'), 'lang:fa'), ('English', 'lang:en')], [(tr('بازگشت'), 'projects')]])

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
        rows.append([(tr('➕ افزودن پروژه'), 'add')])
        rows.append([(tr('🌐 زبان / Language'), 'language')])
        await update.effective_message.reply_text(tr('پروژهٔ هتزنر را انتخاب کنید. هر پروژه توکن مستقل دارد.'), reply_markup=keyboard(rows))

    async def help(self, update, context):
        if not self.authorized(update):
            return
        await update.effective_message.reply_text(tr('/projects — پروژه\u200cها\n/add — افزودن پروژه\n/cancel — لغو ورودی و تأیید\n۱. پروژه\u200cات را انتخاب کن.\n۲. وارد سرورها، اسنپ\u200cشات\u200cها یا IPها شو.\n۳. منبع و کار مورد نظر را انتخاب کن؛ بات مرحله\u200cبه\u200cمرحله راهنمایی می\u200cکند.\nساخت سرور: نام ← کشور ← پلن موجود ← سیستم\u200cعامل ← کلید SSH ← تأیید.\nابزارهای تخصصی در تنظیمات پروژه ← بخش پیشرفته هستند.\nهر تغییر نیازمند تأیید ۲ دقیقه\u200cای است. پاسخ running به معنی پایان عملیات نیست؛ دکمهٔ پیگیری را بزنید. پاسخ محرمانه فقط با درخواست جدا نمایش داده می\u200cشود.'))

    async def add(self, update, context):
        if not self.authorized(update):
            return
        context.user_data.clear()
        context.user_data['input'] = {"kind": "name"}
        await update.effective_message.reply_text(tr('نام دلخواه پروژه را بفرستید؛ /cancel برای لغو.'))

    async def menu(self, update, pid):
        name, _ = await self.project(update, pid)
        await self.show(update, tr('📁 {0}\nچه کاری می\u200cخواهی انجام بدهی؟', name), [[(tr('🖥 سرورهای من'), f'servers:{pid}:1'), (tr('➕ ساخت سرور'), f"new:{pid}:{OPERATIONS.index(BY_ID['create_server'])}")], [(tr('📸 اسنپ\u200cشات و بکاپ'), f'images:{pid}:snapshot:1'), (tr('🌐 آدرس\u200cهای IP'), f'ips:{pid}:1')], [(tr('⚙️ تنظیمات پروژه'), f'settings:{pid}')], [(tr('↩️ تغییر پروژه'), 'projects')]])

    async def ip_menu(self, update, pid, server_id):
        project, token = await self.project(update, pid)
        server = await self.ip_control.server(token, server_id)
        rows = []
        for family in ('ipv4', 'ipv6'):
            rows.append([(tr('تعویض {0}', family.upper()), f'ipset:{pid}:{server_id}:{family}')])
            if (server.get('public_net', {}).get(family) or {}).get('id'):
                rows[-1].append((tr('جداسازی {0}', family.upper()), f'ipdetach:{pid}:{server_id}:{family}'))
        rows += [[(tr('➕ ساخت IP'), f"new:{pid}:{OPERATIONS.index(BY_ID['create_primary_ip'])}")],
                 [(tr('بازیابی IP ناتمام'), f'iprecover:{pid}:{server_id}')], [(tr('بازگشت به سرور'), f'srv:{pid}:{server_id}')]]
        await self.show(update, server_card(server, project) + tr('\n\nبرای تغییر IP، سرور باید خاموش باشد. IP جداشده حذف نمی\u200cشود و ممکن است همچنان هزینه داشته باشد.'), rows)

    async def ip_pick(self, update, context, pid, server_id, family):
        _, token = await self.project(update, pid)
        server = await self.ip_control.server(token, server_id)
        if family not in {'ipv4', 'ipv6'} or server['status'] != 'off' or server.get('locked'):
            raise ValueError(tr('برای تغییر یا جداسازی IP، ابتدا سرور را خاموش کنید.'))
        location = (server.get('datacenter') or {}).get('location', {}).get('name')
        ips = await all_items(self.cloud, token, BY_ID['list_primary_ips'], 'primary_ips')
        options = [ip for ip in ips if ip['type'] == family and ip.get('assignee_id') is None
                   and (ip.get('location') or (ip.get('datacenter') or {}).get('location') or {}).get('name') == location]
        nonce = secrets.token_hex(8)
        context.user_data['input'] = {'kind': 'ip_pick', 'pid': pid, 'server': server_id, 'family': family,
                                      'options': options, 'nonce': nonce, 'expires': time.monotonic() + 600, 'page': 0}
        await self.pick_screen(update, context)

    async def pick_screen(self, update, context):
        state = context.user_data['input']
        state['nonce'] = secrets.token_hex(8)
        prefix = f"choice:{state['nonce']}:"
        page, options = state['page'], state['options']
        rows = [[(f"{item.get('name', item['id'])} · {item.get('ip', '#' + str(item['id']))}", prefix + f'pick:{i}')]
                for i, item in enumerate(options) if page*8 <= i < (page+1)*8]
        nav = []
        if page:
            nav.append((tr('قبلی'), prefix + 'page:-1'))
        if (page+1)*8 < len(options):
            nav.append((tr('بعدی'), prefix + 'page:1'))
        if nav:
            rows.append(nav)
        rows.append([(tr('بازگشت به سرور'), f"srv:{state['pid']}:{state['server']}")])
        await self.show(update, tr('یک گزینهٔ موجود را انتخاب کنید.') if options else tr('گزینهٔ سازگار و آزادی پیدا نشد.'), rows)

    async def ip_preview(self, update, context, pid, server_id, family, new_id):
        project, token = await self.project(update, pid)
        server = await self.ip_control.server(token, server_id)
        if server['status'] != 'off' or server.get('locked'):
            raise ValueError(tr('برای تغییر یا جداسازی IP، ابتدا سرور را خاموش کنید.'))
        old = server.get('public_net', {}).get(family) or {}
        if old.get('id') == new_id:
            raise ValueError(tr('IP انتخاب\u200cشده تغییری ایجاد نمی\u200cکند.'))
        target = await self.ip_control.ip(token, new_id) if new_id is not None else None
        self.reset(context)
        pending = Pending(secrets.token_hex(12), pid, 'ip_change',
                          {'server': server_id, 'family': family, 'new_ip': new_id, 'old_ip': old.get('id')}, time.monotonic() + 120, True)
        context.user_data['pending'] = pending
        await self.show(update, tr('تعویض / جداسازی IP\nپروژه: {0}\nسرور: {1}\nنوع: {2}\nIP قبلی: {3}\nIP جدید: {4}\n\nIP قبلی حذف نمی\u200cشود. تغییر اتصال سرور را قطع می\u200cکند و ممکن است هزینه داشته باشد. انجام شود؟', project, server['name'], family.upper(), old.get('ip', tr('ندارد')), target['ip'] if target else tr('بدون IP')), [[(tr('✅ تأیید و انجام'), f'yes:{pending.nonce}'), (tr('✖️ انصراف'), f'net:{pid}:{server_id}')]])

    async def bind_resource(self, update, context, pid, server_id, category):
        _, token = await self.project(update, pid)
        server = await self.ip_control.server(token, server_id)
        collection = {'volume': 'volumes', 'firewall': 'firewalls', 'lb': 'load_balancers'}[category]
        items = await all_items(self.cloud, token, BY_ID['list_' + collection], collection)
        if category == 'volume':
            location = (server.get('datacenter') or {}).get('location', {}).get('name')
            items = [item for item in items if item.get('assignee') is None and item.get('server') is None and location
                     and (item.get('location') or {}).get('name') == location]
        elif category == 'lb':
            items = [item for item in items if not any(target.get('type') == 'server'
                     and (target.get('server') or {}).get('id') == server_id for target in item.get('targets', []))]
        elif category == 'firewall':
            items = [item for item in items if not any(target.get('type') == 'server'
                     and (target.get('server') or {}).get('id') == server_id for target in item.get('applied_to', []))]
        context.user_data['input'] = {'kind': 'resource_bind', 'pid': pid, 'server': server_id,
                                      'category': category, 'options': items, 'page': 0,
                                      'expires': time.monotonic() + 600}
        await self.pick_screen(update, context)

    async def choose_resource(self, update, context, data):
        state = context.user_data.get('input', {})
        if state.get('kind') not in {'ip_pick', 'resource_bind'} or state['expires'] < time.monotonic():
            raise ValueError(tr('این مرحله منقضی شده؛ دوباره شروع کنید.'))
        if not secrets.compare_digest(state['nonce'], data[1]):
            raise ValueError(tr('این دکمه قدیمی است؛ از آخرین پیام استفاده کنید.'))
        await self.project(update, state['pid'])
        if data[2] == 'page':
            state['page'] = max(0, state['page'] + int(data[3]))
            await self.pick_screen(update, context)
            return
        item = state['options'][int(data[3])]
        if state['kind'] == 'ip_pick':
            await self.ip_preview(update, context, state['pid'], state['server'], state['family'], item['id'])
            return
        server_id = state['server']
        operation, body = {
            'volume': ('attach_volume', {'assignee': {'type': 'server', 'id': server_id}}),
            'firewall': ('apply_firewall_to_resources', {'apply_to': [{'type': 'server', 'server': {'id': server_id}}]}),
            'lb': ('add_load_balancer_target', {'type': 'server', 'server': {'id': server_id}}),
        }[state['category']]
        await self.preview(update, context, state['pid'], BY_ID[operation], {'path': {'id': item['id']}, 'body': body}, simple=True, labels={tr('سرور'): f'#{server_id}', tr('منبع'): item.get('name', str(item['id']))})

    async def simple_screen(self, update, context, pid, kind, args):
        project, token = await self.project(update, pid)
        def action(label, operation, resource_id=None):
            index = OPERATIONS.index(BY_ID[operation])
            return (label, f'do:{pid}:{index}:{resource_id}' if resource_id is not None else f'new:{pid}:{index}')
        if kind == 'sub':
            server_id, section = int(args[0]), args[1]
            server = (await self.cloud.execute(token, BY_ID['get_server'], {'path': {'id': server_id}}))['server']
            if section == 'graphs':
                await self.show(update, tr('نمودارهای {0}\nبازهٔ یک ساعت گذشته را انتخاب کنید.', server['name']), [[('CPU', f'metric:{pid}:{server_id}:cpu'), ('Network', f'metric:{pid}:{server_id}:network')], [('Disk', f'metric:{pid}:{server_id}:disk')], [(tr('بازگشت به سرور'), f'srv:{pid}:{server_id}')]])
                return
            if section == 'backup':
                rows = [[action(tr('📸 ساخت اسنپ\u200cشات'), 'create_server_image', server_id)],
                        [action(tr('فعال کردن بکاپ خودکار'), 'enable_server_backup', server_id),
                         action(tr('خاموش کردن بکاپ خودکار'), 'disable_server_backup', server_id)],
                        [(tr('📂 اسنپ\u200cشات\u200cها'), f'images:{pid}:snapshot:1'), (tr('📂 بکاپ\u200cها'), f'images:{pid}:backup:1')]]
            elif section == 'settings':
                rows = [[action(tr('✏️ تغییر نام'), 'update_server', server_id), action(tr('⬆️ تغییر پلن'), 'change_server_type', server_id)],
                        [action(tr('🛠 نصب مجدد / بازیابی'), 'rebuild_server', server_id)],
                        [action(tr('🔑 بازنشانی گذرواژه'), 'reset_server_password', server_id)],
                        [action(tr('فعال کردن Rescue'), 'enable_server_rescue', server_id),
                         action(tr('غیرفعال کردن Rescue'), 'disable_server_rescue', server_id)],
                        [action(tr('قطع برق اجباری'), 'poweroff_server', server_id), action(tr('🗑 حذف سرور'), 'delete_server', server_id)],
                        [(tr('تنظیمات تخصصی'), f'item:{pid}:0:{server_id}:0')]]
            else:
                groups = {
                    'power': ['poweron_server', 'shutdown_server', 'reboot_server', 'poweroff_server', 'reset_server'],
                    'rescue': ['enable_server_rescue', 'disable_server_rescue', 'reset_server_password'],
                    'iso': ['attach_server_iso', 'detach_server_iso'],
                    'network': ['attach_server_to_network', 'detach_server_from_network', 'change_server_dns_ptr', 'change_server_alias_ips'],
                    'scale': ['change_server_type'], 'rebuild': ['rebuild_server'], 'delete': ['delete_server'],
                    'graphs': ['get_server_metrics'],
                }
                rows = []
                if section in groups:
                    for operation in groups[section]:
                        if operation in {'change_server_alias_ips', 'get_server_metrics'}:
                            rows.append([(title(BY_ID[operation]), f"edit:{pid}:{OPERATIONS.index(BY_ID[operation])}:{server_id}")])
                        else:
                            rows.append([action(title(BY_ID[operation]), operation, server_id)])
                    if section == 'network':
                        rows.insert(0, [(tr('🌐 جداسازی و تعویض IP'), f'net:{pid}:{server_id}')])
                elif section == 'volume':
                    items = server.get('volumes', [])
                    rows = [[(tr('دیسک #{0}', value), f'item:{pid}:4:{value}:0')] for value in items]
                    rows += [[(tr('اتصال یک دیسک'), f'bind:{pid}:{server_id}:volume')]]
                elif section == 'firewall':
                    items = (server.get('public_net') or {}).get('firewalls', [])
                    rows = [[(tr('فایروال #{0}', value['id']), f"item:{pid}:3:{value['id']}:0")] for value in items]
                    rows += [[(tr('اعمال فایروال روی این سرور'), f'bind:{pid}:{server_id}:firewall')]]
                elif section == 'lb':
                    balancers = await all_items(self.cloud, token, BY_ID['list_load_balancers'], 'load_balancers')
                    rows = [[(item['name'], f"item:{pid}:8:{item['id']}:0")] for item in balancers
                            if any(target.get('type') == 'server' and (target.get('server') or {}).get('id') == server_id
                                   for target in item.get('targets', []))]
                    rows.append([(tr('افزودن این سرور به Load Balancer'), f'bind:{pid}:{server_id}:lb')])
                else:
                    raise ValueError(tr('بخش انتخاب\u200cشده معتبر نیست.'))
            rows.append([(tr('بازگشت به سرور'), f'srv:{pid}:{server_id}')])
            section_names = {'backup': tr('اسنپ‌شات و بکاپ'), 'network': tr('🌐 شبکه و IP'),
                             'firewall': tr('🛡 فایروال'), 'volume': tr('💾 دیسک‌ها'), 'lb': 'Load Balancers',
                             'iso': 'ISO', 'rescue': 'Rescue', 'power': 'Power',
                             'scale': title(BY_ID['change_server_type']), 'rebuild': title(BY_ID['rebuild_server']),
                             'delete': title(BY_ID['delete_server'])}
            await self.show(update, tr('{0}\nپروژه: {1}\n{2}', server['name'], project,
                                      section_names.get(section, tr('تغییرات سرور'))), rows)
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
                    label += f" · {item.get('ip', tr('در حال تخصیص'))}"
                rows.append([(label, f"{'pic' if kind == 'images' else 'ip'}:{pid}:{item['id']}")])
            nav = []
            prefix = f'images:{pid}:{args[0]}' if kind == 'images' else f'ips:{pid}'
            if page > 1:
                nav.append((tr('قبلی'), f'{prefix}:{page - 1}'))
            if result.get('meta', {}).get('pagination', {}).get('next_page'):
                nav.append((tr('بعدی'), f'{prefix}:{page + 1}'))
            if nav:
                rows.append(nav)
            if kind == 'images':
                rows += [[(tr('📸 اسنپ\u200cشات\u200cها'), f'images:{pid}:snapshot:1'), (tr('بکاپ\u200cهای خودکار'), f'images:{pid}:backup:1')],
                         [action(tr('➕ ساخت اسنپ\u200cشات'), 'create_server_image')]]
                heading = tr('اسنپ\u200cشات\u200cها') if args[0] == 'snapshot' else tr('بکاپ\u200cهای خودکار')
            else:
                rows.append([action(tr('➕ ساخت IP'), 'create_primary_ip')])
                heading = tr('آدرس\u200cهای IP')
            rows.append([(tr('🔄 تازه\u200cسازی'), f'{prefix}:{page}'), (tr('بازگشت'), f'p:{pid}')])
            await self.show(update, tr('{0}\nپروژه: {1}\n', heading, project) + (tr('یک مورد را انتخاب کن.') if result[collection] else tr('هنوز موردی وجود ندارد.')), rows)
            return
        resource_id = int(args[0])
        if kind == 'pic':
            item = (await self.cloud.execute(token, BY_ID['get_image'], {'path': {'id': resource_id}}))['image']
            await self.show(update, tr('📸 {0}\nپروژه: {1}\nاندازه: {2} GB\nوضعیت: {3}', item.get('description') or item.get('name') or resource_id, project, item.get('image_size', '—'), item.get('status', '—')), [[(tr('♻️ بازیابی روی سرور'), f'restore:{pid}:{resource_id}')], [action(tr('✏️ تغییر نام'), 'update_image', resource_id), action(tr('🗑 حذف'), 'delete_image', resource_id)], [(tr('بازگشت'), f'images:{pid}:snapshot:1')]])
        elif kind == 'ip':
            item = (await self.cloud.execute(token, BY_ID['get_primary_ip'], {'path': {'id': resource_id}}))['primary_ip']
            await self.show(update, tr('🌐 {0}\nپروژه: {1}\nIP: {2}\nسرور متصل: {3}', item['name'], project, item.get('ip', '—'), item.get('assignee_id') or tr('متصل نیست')), [[action(tr('اتصال به سرور'), 'assign_primary_ip', resource_id), action(tr('جدا کردن IP'), 'unassign_primary_ip', resource_id)], [action(tr('✏️ تغییر نام'), 'update_primary_ip', resource_id), action(tr('🗑 حذف IP'), 'delete_primary_ip', resource_id)], [(tr('بازگشت'), f'ips:{pid}:1')]])

    async def resources(self, update, pid, collection_index, page):
        name, token = await self.project(update, pid)
        collection, label = COLLECTIONS[collection_index]
        label = tr(label)
        op = next(o for o in OPERATIONS if o['path'] == '/' + collection and o['method'] == 'GET')
        result = await self.cloud.execute(token, op, {"query": {"page": page, "per_page": 8}})
        rows = [[(f"{resource.get('name') or resource.get('description') or resource.get('ip') or resource['id']} · {resource['id']}",
                  f"item:{pid}:{collection_index}:{resource['id']}:0")]
                for resource in result[collection]]
        nav = []
        if page > 1:
            nav.append((tr('قبلی'), f'res:{pid}:{collection_index}:{page - 1}'))
        if result.get('meta', {}).get('pagination', {}).get('next_page'):
            nav.append((tr('بعدی'), f'res:{pid}:{collection_index}:{page + 1}'))
        if nav:
            rows.append(nav)
        create = next((i for i, o in enumerate(OPERATIONS)
                       if o['path'] == '/' + collection and o['method'] == 'POST'), None)
        if create is not None:
            rows.append([(tr('➕ ساخت / افزودن'), f'form:{pid}:{create}')])
        rows.append([(tr('منابع'), f'resources:{pid}')])
        await update.effective_message.reply_text(tr('پروژه: {0}\n{1} — صفحه {2}', name, label, page), reply_markup=keyboard(rows))

    async def resource_item(self, update, pid, collection_index, resource_id, page):
        name, token = await self.project(update, pid)
        collection, label = COLLECTIONS[collection_index]
        label = tr(label)
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
            nav.append((tr('قبلی'), f'item:{pid}:{collection_index}:{resource_id}:{page - 1}'))
        if (page+1)*8 < len(operations):
            nav.append((tr('بعدی'), f'item:{pid}:{collection_index}:{resource_id}:{page + 1}'))
        if nav:
            rows.append(nav)
        rows.append([(tr('بازگشت به فهرست'), f'res:{pid}:{collection_index}:1')])
        await update.effective_message.reply_text(tr('پروژه: {0}\n{1} — شناسه {2}\nعملیات:', name, label, resource_id), reply_markup=keyboard(rows))

    async def ask_operation(self, update, context, pid, index):
        op = OPERATIONS[index]
        name, _ = await self.project(update, pid)
        self.reset(context)
        payload = sample(schema_for(op))
        if any(p["in"] == "query" for p in op["parameters"]):
            payload.setdefault("query", {})
        if not op["parameters"] and not op["requestBody"] and op["method"] == "GET":
            await self.execute(update, context, pid, op, {})
            return
        context.user_data['input'] = {"kind": "operation", "pid": pid, "index": index,
                                      "expires": time.monotonic() + 600}
        note = tr('\n⚠ عملیات منسوخ\u200cشده در API.') if op.get("deprecated") else ""
        await update.effective_message.reply_text(tr('پروژه: {0}\n{1}\n{2} {3}{4}\nنمونهٔ زیر را با مقادیر خودتان ویرایش و به صورت متن یا فایل JSON ارسال کنید. نمونه ممکن است شامل شناسهٔ فرضی باشد. query اختیاری برای فیلتر و صفحه\u200cبندی است.\n/cancel', name, op['summary'], op['method'], op['path'], note), reply_markup=keyboard([[(tr('راهنمای فیلدها (JSON Schema)'), f'schema:{pid}:{index}')]]))
        await self.send_json(update, payload, "example.json")

    async def preview(self, update, context, pid, op, payload, simple=False, labels=None):
        prepare(op, payload)
        name, token = await self.project(update, pid)
        self.reset(context)
        pending = Pending(secrets.token_hex(12), pid, op["id"], payload, time.monotonic() + 120, simple)
        context.user_data['pending'] = pending
        if simple:
            from .forms import FIELDS
            details = dict(labels or {})
            for key, value in payload.get('body', {}).items():
                if key not in details:
                    details[key] = tr('بله') if value is True else tr('خیر') if value is False else str(value)
            if payload.get('path') and tr('سرور') not in details:
                details[tr('شناسهٔ منبع')] = str(next(iter(payload['path'].values())))
            warning = (tr('⚠️ تمام اطلاعات دیسک پاک می\u200cشود.') if op['id'] == 'rebuild_server' else
                       tr('⚠️ حذف منبع دائمی است.') if op['method'] == 'DELETE' else
                       tr('ایجاد یا تغییر منابع می\u200cتواند هزینه داشته باشد و اتصال سرور را قطع کند.'))
            text = tr('{0}\nپروژه: {1}\n\n', title(op), name) + '\n'.join((f'{tr(FIELDS.get(k, k))}: {str(v)[:250]}' for k, v in details.items()))
            await self.show(update, text[:2800] + tr('\n\n{0}\nانجام شود؟', warning), [[(tr('✅ تأیید و انجام'), f'yes:{pending.nonce}'), (tr('✖️ انصراف'), f'p:{pid}')]])
            return
        await update.effective_message.reply_text(tr('⚠ تأیید تغییر — پروژه: {0}\n{1}\n{2} {3}\nاین عملیات ممکن است هزینه ایجاد کند، سرویس را قطع کند یا داده را حذف کند. منبع و مقادیر را بررسی کنید. مهلت: ۲ دقیقه.', name, op['summary'], op['method'], op['path']))
        await self.send_json(update, redact(payload, (token,)))
        await update.effective_message.reply_text(tr('اجرا شود؟'), reply_markup=keyboard([[(tr('✅ تأیید و اجرا'), f'yes:{pending.nonce}'), (tr('لغو'), 'cancel')]]))

    async def execute(self, update, context, pid, op, payload, simple=False):
        name, token = await self.project(update, pid)
        if simple and op['id'] == 'create_server':
            await self.simple.validate_create(token, payload)
        result = await self.cloud.execute(token, op, payload)
        logging.info("cloud_request project=%s operation=%s", pid, op["id"])
        if simple:
            action = result.get('action', {})
            status = action.get('status')
            message = (tr('⏳ درخواست ثبت شد و در حال انجام است.') if status == 'running' else
                       tr('❌ عملیات در هتزنر ناموفق بود.') if status == 'error' else
                       tr('✅ عملیات انجام شد.') if status == 'success' else tr('✅ هتزنر درخواست را پذیرفت.'))
            if isinstance(result.get('server'), dict) and 'name' in result['server']:
                message += tr('\nسرور: {0}\nIPv4: {1}', result['server']['name'], ((result['server'].get('public_net') or {}).get('ipv4') or {}).get('ip', tr('هنوز تخصیص داده نشده')))
        else:
            await update.effective_message.reply_text(tr('پروژه: {0} — پاسخ هتزنر:', name))
            await self.send_json(update, redact(result, (token,)))
        # Sensitive responses are private and opt-in, never logs or persistent storage.
        full_result = redact(result, (token,), hide_fields=False)
        receipt_value = full_result
        if simple:
            receipt_value = {k: v for k, v in full_result.items()
                             if k in {'root_password', 'password', 'wss_url', 'ticket'} and v}
        if (simple and receipt_value) or (not simple and redact(result, (token,)) != full_result):
            nonce = secrets.token_hex(12)
            context.user_data['receipt'] = {
                "nonce": nonce, "expires": time.monotonic() + 120,
                "value": receipt_value, "project": pid, 'simple': simple,
            }
            await update.effective_message.reply_text(tr('پاسخ شامل اطلاعات محرمانه مثل گذرواژه یا کنسول است. برای دریافت پاسخ کامل در همین چت خصوصی، دکمه را بزنید و سپس پیام را دستی حذف کنید. مهلت: ۲ دقیقه.'), reply_markup=keyboard([[(tr('دریافت پاسخ کامل محرمانه'), f'reveal:{nonce}')]]))
        actions = result.get("actions", [])
        if isinstance(result.get("action"), dict):
            actions = [result["action"]] + actions
        rows = [[(tr('پیگیری عملیات {0}', action['id']), f"{'check' if simple else 'action'}:{pid}:{action['id']}")]
                for action in actions[:10] if isinstance(action, dict) and "id" in action]
        rows.append([(tr('بازگشت به پروژه'), f'p:{pid}')])
        nav = context.user_data.get('nav')
        if nav and nav['pid'] == pid and op['id'] != 'delete_server':
            rows.insert(0, [(tr('بازگشت به سرور'), f"srv:{pid}:{nav['server']}")])
        if simple:
            server_id = (result.get('server') or {}).get('id')
            if not server_id and op['path'].startswith('/servers/') and op['method'] != 'DELETE':
                server_id = payload.get('path', {}).get('id')
            if server_id:
                rows.insert(0, [(tr('🖥 نمایش سرور'), f'srv:{pid}:{server_id}')])
        if simple:
            await self.show(update, tr('پروژه: {0}\n{1}', name, message), rows)
        else:
            await update.effective_message.reply_text(tr('ادامه:'), reply_markup=keyboard(rows))

    async def servers(self, update, pid, page):
        name, token = await self.project(update, pid)
        result = await self.cloud.execute(token, BY_ID["list_servers"],
                                          {"query": {"page": page, "per_page": 10}})
        rows = [[(f"{tr(STATUS.get(server['status'], server['status']))} · {server['name']}", f"srv:{pid}:{server['id']}")]
                for server in result["servers"] if server.get('status') != 'deleting']
        nav = []
        if page > 1:
            nav.append((tr('قبلی'), f'servers:{pid}:{page - 1}'))
        if result.get("meta", {}).get("pagination", {}).get("next_page"):
            nav.append((tr('بعدی'), f'servers:{pid}:{page + 1}'))
        if nav:
            rows.append(nav)
        rows.append([(tr('➕ ساخت سرور'), f"new:{pid}:{OPERATIONS.index(BY_ID['create_server'])}")])
        rows.append([(tr('🔄 تازه\u200cسازی'), f'servers:{pid}:{page}'), (tr('بازگشت'), f'p:{pid}')])
        text = tr('🖥 سرورهای {0}\nسرور مورد نظر را انتخاب کن.', name)
        if not result['servers']:
            text = tr('🖥 {0}\nهنوز سروری در این پروژه نیست. از «ساخت سرور» شروع کن.', name)
        await self.show(update, text, rows)

    async def callback(self, update, context):
        if not self.authorized(update):
            if update.callback_query:
                await update.callback_query.answer(tr('دسترسی مجاز نیست.'), show_alert=True)
            return
        await update.callback_query.answer()
        data = update.callback_query.data.split(":")
        kind = data[0]
        if kind == 'language':
            await self.language(update, context)
            return
        if kind == 'lang':
            language = data[1]
            await asyncio.to_thread(self.store.set_language, update.effective_user.id, language)
            self.languages[update.effective_user.id] = language
            LANGUAGE.set(language)
            await self.projects(update, context)
            return
        if kind == 'choice':
            await self.choose_resource(update, context, data)
            return
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
                raise ValueError(tr('این پاسخ دیگر در دسترس نیست.'))
            context.user_data.pop("receipt")
            if receipt["expires"] < time.monotonic():
                raise ValueError(tr('مهلت دریافت پاسخ محرمانه تمام شده است.'))
            name, _ = await self.project(update, receipt["project"])
            await update.effective_message.reply_text(tr('⚠ پاسخ کامل محرمانه — پروژه: {0}', name))
            if receipt.get('simple'):
                names = {'root_password': tr('گذرواژهٔ سرور'), 'password': tr('گذرواژه'),
                         'wss_url': tr('آدرس کنسول'), 'ticket': tr('کلید اتصال کنسول')}
                await update.effective_message.reply_text('\n'.join(
                    f"{names.get(k, k)}: {v}" for k, v in receipt['value'].items()))
            else:
                await self.send_json(update, receipt["value"], "sensitive-result.json")
            return
        if kind == "yes":
            pending = consume_pending(context.user_data, data[1])
            if pending.operation == 'ip_change':
                await self.project(update, pending.project)
                await self.show(update, tr('در حال تغییر IP؛ لطفاً صبر کنید و سرور را خاموش نگه دارید.'), [])
                await self.ip_control.change(update.effective_user.id, pending.project, pending.payload['server'],
                                             pending.payload['family'], pending.payload['new_ip'], pending.payload['old_ip'])
                await self.ip_menu(update, pending.project, pending.payload['server'])
            elif pending.operation == "forget":
                await asyncio.to_thread(self.store.remove, update.effective_user.id, pending.project)
                await self.projects(update, context)
            else:
                await self.execute(update, context, pending.project, BY_ID[pending.operation], pending.payload,
                                   simple=pending.simple)
            return
        pid = data[1]
        name, token = await self.project(update, pid)
        if kind not in {"schema"}:
            if kind in {'p', 'resources', 'servers', 'settings', 'advanced'}:
                context.user_data.clear()
            else:
                self.reset(context)
            if kind in {'srv', 'sub', 'net', 'ipset', 'ipdetach', 'bind'}:
                context.user_data['nav'] = {'pid': pid, 'server': int(data[2])}
        if kind == "p":
            await self.menu(update, pid)
        elif kind in {'new', 'do'}:
            await self.simple.start(update, context, pid, OPERATIONS[int(data[2])]['id'],
                                    data[3] if kind == 'do' else None)
        elif kind == 'restore':
            await self.simple.start(update, context, pid, 'rebuild_server', image_id=int(data[2]))
        elif kind == 'net':
            await self.ip_menu(update, pid, int(data[2]))
        elif kind == 'metric':
            from .graphs import render_chart
            server_id, metric = int(data[2]), data[3]
            if metric not in {'cpu', 'disk', 'network'}:
                raise ValueError(tr('نوع نمودار معتبر نیست.'))
            server = await self.ip_control.server(token, server_id)
            end = datetime.now(timezone.utc)
            result = await self.cloud.execute(token, BY_ID['get_server_metrics'], {
                'path': {'id': server_id}, 'query': {'type': [metric], 'start': (end-timedelta(hours=1)).isoformat(),
                                                  'end': end.isoformat(), 'step': '60'}})
            series = list(result['metrics'].get('time_series', {}).items())
            chunks = [dict(series[i:i+4]) for i in range(0, len(series), 4)] if metric == 'disk' else [dict(series)]
            if not chunks:
                chunks = [{}]
            for index, chunk in enumerate(chunks):
                png = await asyncio.to_thread(render_chart, metric, {'time_series': chunk})
                await update.effective_message.reply_photo(io.BytesIO(png), caption=f"{server['name']} · {metric.upper()} · UTC · {index+1}/{len(chunks)}")
            await self.simple_screen(update, context, pid, 'sub', [server_id, 'graphs'])
        elif kind == 'ipset':
            await self.ip_pick(update, context, pid, int(data[2]), data[3])
        elif kind == 'ipdetach':
            await self.ip_preview(update, context, pid, int(data[2]), data[3], None)
        elif kind == 'iprecover':
            for row in await asyncio.to_thread(self.store.ip_changes):
                if row[0] == update.effective_user.id and row[1] == pid and row[2] == int(data[2]):
                    if not await self.ip_control.recover(row):
                        raise ValueError(tr('بازیابی هنوز ممکن نیست؛ وضعیت سرور و IPها را در هتزنر بررسی کنید.'))
            await self.ip_menu(update, pid, int(data[2]))
        elif kind == 'bind':
            await self.bind_resource(update, context, pid, int(data[2]), data[3])
        elif kind == 'settings':
            await self.show(update, tr('⚙️ تنظیمات {0}', name), [[(tr('🌐 زبان / Language'), 'language')], [(tr('🧰 بخش پیشرفته'), f'advanced:{pid}')], [(tr('حذف اتصال این پروژه از بات'), f'forget:{pid}')], [(tr('بازگشت'), f'p:{pid}')]])
        elif kind == 'advanced':
            await self.show(update, tr('🧰 ابزارهای پیشرفتهٔ {0}\nبرای کارهای روزمره از منوی اصلی استفاده کن.', name), [[(tr('شبکه، فایروال، دیسک، DNS و سایر منابع'), f'resources:{pid}')], [(tr('تمام عملیات و ورودی تخصصی'), f'tags:{pid}:0')], [(tr('بازگشت'), f'p:{pid}')]])
        elif kind in {'images', 'ips', 'pic', 'ip', 'sub'}:
            await self.simple_screen(update, context, pid, kind, data[2:])
        elif kind == "resources":
            rows = [[(label, f"res:{pid}:{i}:1")] for i, (_, label) in enumerate(COLLECTIONS)]
            rows.append([(tr('بازگشت'), f'p:{pid}')])
            await update.effective_message.reply_text(tr('پروژه: {0} — منابع', name), reply_markup=keyboard(rows))
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
            context.user_data['pending'] = Pending(nonce, pid, "forget", {}, time.monotonic() + 120)
            await update.effective_message.reply_text(tr('اتصال {0} از بات حذف شود؟ منابع هتزنر حذف نمی\u200cشوند؛ توکن در هتزنر باطل نمی\u200cشود.', name), reply_markup=keyboard([[(tr('حذف اتصال'), f'yes:{nonce}'), (tr('لغو'), 'cancel')]]))
        elif kind == "servers":
            await self.servers(update, pid, int(data[2]))
        elif kind == "srv":
            server = (await self.cloud.execute(token, BY_ID["get_server"],
                                               {"path": {"id": int(data[2])}}))["server"]
            def action(label, op):
                return (label, f"do:{pid}:{OPERATIONS.index(BY_ID[op])}:{server['id']}")
            await self.show(update, server_card(server, name), [[action(tr('▶️ روشن کردن'), 'poweron_server'), action(tr('⏹ خاموش کردن'), 'shutdown_server')], [action(tr('🔄 ری\u200cاستارت'), 'reboot_server')], [(tr('📸 اسنپ\u200cشات و بکاپ'), f"sub:{pid}:{server['id']}:backup")], [(tr('⚙️ تغییرات سرور'), f"sub:{pid}:{server['id']}:settings")], [(tr('📊 نمودارها'), f"sub:{pid}:{server['id']}:graphs"), (tr('🌐 شبکه و IP'), f"sub:{pid}:{server['id']}:network")], [(tr('🛡 فایروال'), f"sub:{pid}:{server['id']}:firewall"), (tr('💾 دیسک\u200cها'), f"sub:{pid}:{server['id']}:volume")], [('⚖️ Load Balancer', f"sub:{pid}:{server['id']}:lb"), ('💿 ISO', f"sub:{pid}:{server['id']}:iso")], [('🛟 Rescue', f"sub:{pid}:{server['id']}:rescue"), (tr('⬆️ تغییر پلن'), f"sub:{pid}:{server['id']}:scale")], [(tr('🛠 نصب مجدد'), f"sub:{pid}:{server['id']}:rebuild"), (tr('🗑 حذف'), f"sub:{pid}:{server['id']}:delete")], [(tr('🔄 تازه\u200cسازی'), f"srv:{pid}:{server['id']}"), (tr('بازگشت'), f'servers:{pid}:1')]])
        elif kind == "quick":
            operation, body = {
                "on": ("poweron_server", None), "stop": ("shutdown_server", None),
                "reboot": ("reboot_server", None), "off": ("poweroff_server", None),
                "snap": ("create_server_image", {"type": "snapshot"}),
                "del": ("delete_server", None),
            }[data[3]]
            payload = {"path": {"id": int(data[2])}}
            if body is not None:
                payload['body'] = body
            await self.preview(update, context, pid, BY_ID[operation], payload)
        elif kind in {"action", "check"}:
            await self.execute(update, context, pid, BY_ID["get_action"], {"path": {"id": int(data[2])}}, simple=kind == 'check')
        elif kind == "tags":
            page = int(data[2])
            rows = [[(tr(LABELS.get(tag, tag)), f"tag:{pid}:{i}:0")]
                    for i, tag in enumerate(TAGS) if page * 10 <= i < (page + 1) * 10]
            nav = []
            if page:
                nav.append((tr('قبلی'), f'tags:{pid}:{page - 1}'))
            if (page + 1) * 10 < len(TAGS):
                nav.append((tr('بعدی'), f'tags:{pid}:{page + 1}'))
            if nav:
                rows.append(nav)
            rows.append([(tr('بازگشت'), f'p:{pid}')])
            await update.effective_message.reply_text(tr('پروژه: {0} — گروه عملیات', name), reply_markup=keyboard(rows))
        elif kind == "tag":
            tag_index, page = int(data[2]), int(data[3])
            ops = [(i, op) for i, op in enumerate(OPERATIONS) if op["tag"] == TAGS[tag_index]]
            rows = [[(title(op), f"form:{pid}:{i}"), ("JSON", f"op:{pid}:{i}")]
                    for i, op in ops[page*8:(page+1)*8]]
            nav = []
            if page:
                nav.append((tr('قبلی'), f'tag:{pid}:{tag_index}:{page - 1}'))
            if (page+1)*8 < len(ops):
                nav.append((tr('بعدی'), f'tag:{pid}:{tag_index}:{page + 1}'))
            if nav:
                rows.append(nav)
            rows.append([(tr('گروه\u200cها'), f'tags:{pid}:0')])
            await update.effective_message.reply_text(tr('پروژه: {0} — {1}', name, TAGS[tag_index]), reply_markup=keyboard(rows))
        elif kind == "schema":
            await self.send_json(update, schema_for(OPERATIONS[int(data[2])]), "input-schema.json")
        elif kind == "op":
            await self.ask_operation(update, context, pid, int(data[2]))

    async def text(self, update, context, supplied_text=None):
        if not self.authorized(update):
            return
        state = context.user_data.get("input")
        if not state:
            await update.effective_message.reply_text(tr('از /projects شروع کنید.'))
            return
        text = supplied_text if supplied_text is not None else update.effective_message.text.strip()
        if state['kind'] == 'simple':
            await self.simple.text(update, context, text)
            return
        if state["kind"] == "form":
            try:
                await update.effective_message.delete()
            except TelegramError:
                await update.effective_message.reply_text(tr('اگر ورودی محرمانه است، پیام را دستی حذف کنید.'))
            form_text = supplied_text if supplied_text is not None else update.effective_message.text
            await self.forms.text(update, context, form_text)
            return
        if state["kind"] == "name":
            if not text or len(text) > 60:
                raise ValueError(tr('نام پروژه باید بین ۱ تا ۶۰ حرف باشد.'))
            context.user_data['input'] = {"kind": "token", "name": text,
                                          "expires": time.monotonic() + 600}
            await update.effective_message.reply_text(tr('توکن API این پروژه را بفرستید. پیام حاوی توکن را حذف می\u200cکنم و توکن رمزگذاری می\u200cشود. چت بات تلگرام رمزگذاری سرتاسری ندارد؛ برای وارد کردن توکن بدون تلگرام از دستور add-project در راهنما استفاده کنید. /cancel'))
        elif state["kind"] == "token":
            try:
                await update.effective_message.delete()
            except TelegramError:
                await update.effective_message.reply_text(tr('حذف پیام ممکن نشد؛ پیام توکن را دستی حذف کنید.'))
            if state["expires"] < time.monotonic():
                context.user_data.clear()
                raise ValueError(tr('مهلت ورود توکن تمام شد؛ /add'))
            if not text.isascii() or not text.isalnum() or not 32 <= len(text) <= 256:
                raise ValueError(tr('قالب توکن نامعتبر است؛ توکن را دوباره بفرستید.'))
            await self.cloud.execute(text, BY_ID["list_servers"], {"query": {"per_page": 1}})
            try:
                pid = await asyncio.to_thread(self.store.add, update.effective_user.id, state["name"], text)
            except sqlite3.IntegrityError:
                raise ValueError(tr('این نام پروژه قبلاً ثبت شده؛ با /add نام دیگری انتخاب کنید.')) from None
            context.user_data.clear()
            await self.menu(update, pid)
        else:
            # JSON bodies may include passwords or SSH material. Remove the original message too.
            try:
                await update.effective_message.delete()
            except TelegramError:
                await update.effective_message.reply_text(tr('اگر ورودی محرمانه است، پیام را دستی حذف کنید.'))
            if state["expires"] < time.monotonic():
                context.user_data.clear()
                raise ValueError(tr('مهلت ورودی تمام شده؛ عملیات را دوباره انتخاب کنید.'))
            try:
                payload = json.loads(text)
            except ValueError:
                raise ValueError(tr('JSON معتبر نیست؛ از کوتیشن انگلیسی استفاده کنید.')) from None
            op = OPERATIONS[state["index"]]
            prepare(op, payload)
            if op["method"] == "GET":
                self.reset(context)
                await self.execute(update, context, state["pid"], op, payload)
            else:
                await self.preview(update, context, state["pid"], op, payload)

    async def document(self, update, context):
        if not self.authorized(update):
            return
        state = context.user_data.get("input", {})
        if state.get("kind") != "operation" and not (
                state.get("kind") == "form" and self.forms.accepts_document(context)):
            raise ValueError(tr('اول عملیات JSON یا یک فیلد متنی فرم را انتخاب کنید؛ سپس فایل بفرستید.'))
        document = update.effective_message.document
        if not document.file_size or document.file_size > 512 * 1024:
            raise ValueError(tr('حداکثر حجم فایل ورودی برابر ۵۱۲ کیلوبایت است.'))
        file = await document.get_file()
        raw = await file.download_as_bytearray()
        if len(raw) > 512 * 1024:
            raise ValueError(tr('فایل بیش از حد بزرگ است.'))
        try:
            value = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError(tr('فایل باید کدگذاری UTF-8 داشته باشد.')) from None
        await self.text(update, context, supplied_text=value)

    async def error(self, update, context):
        # Never log update objects, exception messages, HTTP URLs or tracebacks: tokens may be present.
        logging.error("handler_failure type=%s", type(context.error).__name__)
        if isinstance(update, Update) and self.authorized(update):
            message = (str(context.error) if isinstance(context.error, (ValueError, CloudError))
                       else tr('خطای داخلی؛ عملیات خودکار تکرار نشد. وضعیت منبع را بررسی کنید.'))
            if isinstance(context.error, CloudError):
                if 'not_found' in message:
                    message = tr('این منبع دیگر در پروژه پیدا نشد. به فهرست برگرد و «تازه\u200cسازی» را بزن.')
                elif 'resource_unavailable' in message:
                    message = tr('ظرفیت پلن انتخاب\u200cشده تمام شده است. دوباره از فهرست پلن\u200cهای موجود انتخاب کن.')
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
        for row in await asyncio.to_thread(store.ip_changes):
            if row[0] not in allowed:
                continue
            LANGUAGE.set(manager.languages.get(row[0], 'fa'))
            try:
                outcome = await manager.ip_control.recover(row)
                text = tr('تغییر IP ناتمام بررسی و بازیابی شد.') if outcome else tr('تغییر IP ناتمام است؛ سرور را خاموش نگه دارید و بازیابی IP را بررسی کنید.')
            except Exception:
                logging.error('ip_recovery_failed project=%s server=%s', row[1], row[2])
                text = tr('بازیابی IP انجام نشد؛ وضعیت سرور را بررسی کنید.')
            try:
                await application.bot.send_message(row[0], text)
            except TelegramError:
                logging.error('ip_recovery_notification_failed owner=%s', row[0])
        application.bot_data['cleanup'] = asyncio.create_task(cleanup(application), name="expire-private-state")

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
    application.add_handler(CommandHandler("language", manager.language))
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
