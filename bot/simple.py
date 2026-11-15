"""Task-oriented screens and sequential questions for everyday server management."""
import secrets
import time

from .api import BY_ID, prepare
from .availability import all_items, available_plan
from .forms import scalar, title

STATUS = {'running': '🟢 روشن', 'off': '🔴 خاموش', 'starting': '🟡 در حال روشن شدن',
          'stopping': '🟡 در حال خاموش شدن', 'initializing': '🟡 در حال آماده‌سازی',
          'rebuilding': '🟡 در حال نصب مجدد', 'deleting': 'در حال حذف'}


def server_card(server, project):
    public = server.get('public_net') or {}
    plan = server.get('server_type') or {}
    location = (server.get('datacenter') or {}).get('location') or {}
    image = server.get('image') or {}
    gb = lambda n: f'{(n or 0) / 1024**3:.1f}'
    return (f"🖥 {server['name']}\nپروژه: {project}\nوضعیت: {STATUS.get(server['status'], server['status'])}\n\n"
            f"IPv4: {(public.get('ipv4') or {}).get('ip', 'ندارد')}\n"
            f"IPv6: {(public.get('ipv6') or {}).get('ip', 'ندارد')}\n"
            f"موقعیت: {location.get('city') or location.get('name', '—')}\n"
            f"پلن: {plan.get('name', '—')} · {plan.get('cores', '—')} هسته · {plan.get('memory', '—')} GB RAM\n"
            f"دیسک: {server.get('disk', '—')} GB\n"
            f"سیستم‌عامل: {image.get('description') or image.get('name', '—')}\n"
            f"ترافیک خروجی: {gb(server.get('outgoing_traffic'))} GB\n"
            f"بکاپ خودکار: {'فعال' if server.get('backup_window') else 'غیرفعال'}\n"
            f"شناسه: {server['id']}")


class SimpleUI:
    def __init__(self, manager):
        self.manager = manager

    async def start(self, update, context, pid, operation, resource_id=None, image_id=None):
        _, token = await self.manager.project(update, pid)
        context.user_data.clear()
        path, body, labels = {}, {}, {}
        server = None
        ip_location = None
        if resource_id is not None:
            path = {'id': int(resource_id)}
            if '/servers/' in BY_ID[operation]['path']:
                server = (await self.manager.cloud.execute(token, BY_ID['get_server'], {'path': path}))['server']
                labels['سرور'] = f"{server['name']} (#{server['id']})"
                if operation == 'change_server_type' and server['status'] != 'off':
                    raise ValueError('برای تغییر پلن، ابتدا سرور را خاموش کن و سپس دوباره این گزینه را بزن.')
            elif '/primary_ips/' in BY_ID[operation]['path']:
                ip = (await self.manager.cloud.execute(token, BY_ID['get_primary_ip'], {'path': path}))['primary_ip']
                labels['IP'] = f"{ip['name']} · {ip.get('ip', '')}"
                ip_location = (ip.get('location') or (ip.get('datacenter') or {}).get('location') or {}).get('name')
            elif '/images/' in BY_ID[operation]['path']:
                image = (await self.manager.cloud.execute(token, BY_ID['get_image'], {'path': path}))['image']
                labels['اسنپ‌شات'] = image.get('description') or image.get('name') or str(resource_id)
        steps = {
            'create_server': [('name', 'text', 'برای سرور یک نام انگلیسی بنویس؛ مثلاً my-server.'),
                              ('location', 'location', 'سرور در کدام کشور / شهر ساخته شود؟'),
                              ('server_type', 'plan', 'یکی از پلن‌های موجود در همین موقعیت را انتخاب کن.'),
                              ('image', 'image', 'سیستم‌عامل سرور را انتخاب کن.'),
                              ('ssh_keys', 'ssh', 'کلید SSH را انتخاب کن یا از این مرحله بگذر.')],
            'update_server': [('name', 'text', 'نام جدید سرور را به انگلیسی بنویس.')],
            'create_server_image': [('description', 'text', 'یک نام برای این اسنپ‌شات بنویس.')],
            'rebuild_server': [('image', 'restore', 'سیستم‌عامل یا بکاپ برای نصب مجدد را انتخاب کن. همهٔ اطلاعات دیسک پاک می‌شود.')],
            'change_server_type': [('server_type', 'plan', 'پلن جدید را انتخاب کن. سرور باید خاموش باشد.'),
                                   ('upgrade_disk', 'disk', 'دیسک هم بزرگ شود؟ افزایش اندازهٔ دیسک برگشت‌پذیر نیست.')],
            'create_primary_ip': [('name', 'text', 'برای IP یک نام انگلیسی بنویس.'),
                                  ('type', 'family', 'نوع IP را انتخاب کن.'),
                                  ('location', 'ip_location', 'موقعیت IP را انتخاب کن.')],
            'assign_primary_ip': [('assignee_id', 'server', 'IP به کدام سرور متصل شود؟ سرور باید خاموش باشد.')],
            'update_primary_ip': [('name', 'text', 'نام جدید IP را به انگلیسی بنویس.')],
            'update_image': [('description', 'text', 'نام جدید اسنپ‌شات را بنویس.')],
        }.get(operation, [])
        image_arch = None
        if image_id is not None:
            image = (await self.manager.cloud.execute(token, BY_ID['get_image'], {'path': {'id': int(image_id)}}))['image']
            body['image'] = str(image_id)
            labels['image'] = image.get('description') or str(image_id)
            image_arch = image.get('architecture')
            steps = []
        if operation in {'create_server_image', 'rebuild_server'} and resource_id is None:
            steps = [('target', 'server', 'کدام سرور؟')] + steps
        if operation == 'create_server_image':
            body['type'] = 'snapshot'
        if operation == 'enable_server_rescue':
            body['type'] = 'linux64'
        if operation in {'create_primary_ip', 'assign_primary_ip'}:
            body['assignee_type'] = 'server'
        state = {'kind': 'simple', 'pid': pid, 'operation': operation, 'path': path, 'body': body,
                 'labels': labels, 'steps': steps, 'step': 0, 'page': 0,
                 'expires': time.monotonic() + 900, 'server': server, 'image_arch': image_arch,
                 'ip_location': ip_location}
        context.user_data['input'] = state
        await self.render(update, context)

    def state(self, context):
        state = context.user_data.get('input', {})
        if state.get('kind') != 'simple' or state['expires'] < time.monotonic():
            raise ValueError('این مرحله منقضی شده؛ از منوی پروژه دوباره شروع کن.')
        return state

    async def choices(self, state, token, mode):
        cloud = self.manager.cloud
        async def listing(collection, query=None):
            return await all_items(cloud, token, BY_ID['list_' + collection], collection, query)
        body = state['body']
        if mode == 'location':
            plans = await listing('server_types')
            return [(x['name'], f"{x.get('country', '')} · {x.get('city', x['name'])}", x)
                    for x in await listing('locations') if any(available_plan(p, x['name']) for p in plans)]
        if mode == 'ip_location':
            return [(x['name'], f"{x.get('country', '')} · {x.get('city', x['name'])}", x)
                    for x in await listing('locations')]
        if mode == 'plan':
            location = body.get('location') or (((state.get('server') or {}).get('datacenter') or {}).get('location') or {}).get('name')
            architecture = ((state.get('server') or {}).get('server_type') or {}).get('architecture')
            plans = [p for p in await listing('server_types') if available_plan(p, location)
                     and (not architecture or p.get('architecture') == architecture)]
            result = []
            pricing = await cloud.execute(token, BY_ID['get_pricing'], {})
            currency = (pricing.get('pricing') or {}).get('currency')
            for plan in plans:
                price = next((p for p in plan.get('prices', []) if p['location'] == location), {})
                amount = (price.get('price_monthly') or {}).get('gross')
                label = f"{plan['name']} · {plan.get('cores', '?')} هسته · {plan.get('memory', '?')} GB RAM"
                if amount and currency:
                    label += f" · {amount} {currency}/ماه"
                result.append((plan['name'], label, plan))
            return result
        if mode in {'image', 'restore'}:
            architecture = state.get('architecture') or ((state.get('server') or {}).get('server_type') or {}).get('architecture')
            query = {'type': ['system'] if mode == 'image' else ['system', 'snapshot', 'backup']}
            if architecture:
                query['architecture'] = architecture
            return [(str(x['id']), x.get('description') or x.get('name') or str(x['id']), x)
                    for x in await listing('images', query) if not x.get('deprecated')]
        if mode == 'ssh':
            return [([x['id']], x['name'], x) for x in await listing('ssh_keys')]
        if mode == 'datacenter':
            return [(x['name'], f"{x['location'].get('city', '')} · {x['name']}", x)
                    for x in await listing('datacenters')]
        if mode == 'server':
            return [(x['id'], f"{x['name']} · {STATUS.get(x['status'], x['status'])}", x)
                    for x in await listing('servers') if x.get('status') != 'deleting'
                    and (state['operation'] != 'assign_primary_ip' or x.get('status') == 'off')
                    and (not state.get('ip_location') or ((x.get('datacenter') or {}).get('location') or {}).get('name') == state['ip_location'])
                    and (not state.get('image_arch') or (x.get('server_type') or {}).get('architecture') == state['image_arch'])]
        if mode == 'family':
            return [('ipv4', 'IPv4 (دارای هزینه)', {}), ('ipv6', 'IPv6', {})]
        if mode == 'disk':
            return [(False, 'فقط CPU و RAM؛ دیسک تغییر نکند', {}),
                    (True, 'CPU و RAM و دیسک بزرگ‌تر شوند', {})]
        return []

    async def render(self, update, context):
        state = self.state(context)
        project, token = await self.manager.project(update, state['pid'])
        if state['step'] == len(state['steps']):
            payload = {}
            if state['path']:
                payload['path'] = state['path']
            if state['body']:
                payload['body'] = state['body']
            if state['operation'] == 'create_server':
                try:
                    await self.validate_create(token, payload)
                except ValueError:
                    context.user_data.clear()
                    raise
            await self.manager.preview(update, context, state['pid'], BY_ID[state['operation']], payload,
                                       simple=True, labels=state['labels'])
            return
        key, mode, question = state['steps'][state['step']]
        if mode == 'plan':
            question += '\nقیمت مربوط به پلن است؛ IP و سرویس‌های اضافه هزینهٔ جدا دارند.'
        state['nonce'] = secrets.token_hex(8)
        prefix = f"easy:{state['nonce']}:"
        rows = []
        if mode != 'text':
            options = await self.choices(state, token, mode)
            state['options'] = options
            for i in range(state['page']*6, min(len(options), (state['page']+1)*6)):
                rows.append([(options[i][1], prefix + f'pick:{i}')])
            nav = []
            if state['page']:
                nav.append(('قبلی', prefix + 'page:-1'))
            if (state['page']+1)*6 < len(options):
                nav.append(('بعدی', prefix + 'page:1'))
            if nav:
                rows.append(nav)
            if not options:
                question += '\nدر حال حاضر گزینهٔ موجودی پیدا نشد. می‌توانی به مرحلهٔ قبل برگردی.'
            if mode == 'ssh':
                rows.append([('بدون SSH؛ دریافت گذرواژهٔ سرور', prefix + 'skip')])
        if state['step']:
            rows.append([('↩️ مرحلهٔ قبل', prefix + 'back')])
        rows.append([('✖️ انصراف', f"p:{state['pid']}")])
        await self.manager.show(update,
            f"{title(BY_ID[state['operation']])}\nپروژه: {project}\n"
            f"مرحلهٔ {state['step']+1} از {len(state['steps'])}\n\n{question}", rows)

    async def callback(self, update, context, parts):
        state = self.state(context)
        if not secrets.compare_digest(parts[1], state['nonce']):
            raise ValueError('این دکمه مربوط به مرحلهٔ قبلی است؛ از آخرین پیام استفاده کن.')
        _, token = await self.manager.project(update, state['pid'])
        key, mode, _ = state['steps'][state['step']]
        action = parts[2]
        if action == 'page':
            state['page'] = max(0, state['page'] + int(parts[3]))
        elif action == 'back':
            state['step'] -= 1
            state['page'] = 0
            for field, _, _ in state['steps'][state['step']:]:
                state['body'].pop(field, None)
                state['labels'].pop(field, None)
            if any(field == 'server_type' for field, _, _ in state['steps'][state['step']:]):
                state.pop('architecture', None)
        elif action in {'pick', 'skip'}:
            if action == 'skip':
                if mode != 'ssh':
                    raise ValueError('این مرحله قابل رد کردن نیست.')
                state['body'].pop(key, None)
            else:
                value, label, metadata = state['options'][int(parts[3])]
                if mode == 'plan' and not available_plan(metadata, state['body'].get('location')):
                    raise ValueError('این پلن دیگر موجود نیست؛ فهرست را تازه کن.')
                if key == 'target':
                    state['path'] = {'id': value}
                    state['labels']['سرور'] = label
                    state['server'] = metadata
                else:
                    state['body'][key] = value
                    state['labels'][key] = label
                if mode == 'plan':
                    state['architecture'] = metadata.get('architecture')
            state['step'] += 1
            state['page'] = 0
        await self.render(update, context)

    async def text(self, update, context, text):
        state = self.state(context)
        await self.manager.project(update, state['pid'])
        key, mode, _ = state['steps'][state['step']]
        if mode != 'text':
            raise ValueError('برای این مرحله یکی از دکمه‌ها را انتخاب کن.')
        op = BY_ID[state['operation']]
        schema = op['requestBody']['content']['application/json']['schema']['properties'][key]
        value = scalar(text.strip(), schema)
        if key == 'name' and not all(c.isascii() and (c.isalnum() or c in '-.') for c in value):
            raise ValueError('نام باید انگلیسی و شامل حروف، عدد، خط تیره یا نقطه باشد.')
        state['body'][key] = value
        state['labels'][key] = value
        state['step'] += 1
        state['page'] = 0
        await self.render(update, context)

    async def validate_create(self, token, payload):
        body = payload['body']
        plans = await all_items(self.manager.cloud, token, BY_ID['list_server_types'], 'server_types')
        plan = next((p for p in plans if p['name'] == body['server_type']), None)
        if not plan or not available_plan(plan, body['location']):
            raise ValueError('این پلن در موقعیت انتخاب‌شده موجود نیست. دوباره ساخت سرور را شروع کن.')
        prepare(BY_ID['create_server'], payload)
