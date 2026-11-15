"""Task-oriented screens and sequential questions for everyday server management."""
from .i18n import tr
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
    return (tr('🖥 {0}\nپروژه: {1}\nوضعیت: {2}\n\nIPv4: {3}\nIPv6: {4}\nموقعیت: {5}\nپلن: {6} · {7} هسته · {8} GB RAM\nدیسک: {9} GB\nسیستم\u200cعامل: {10}\nترافیک خروجی: {11} GB\nبکاپ خودکار: {12}\nشناسه: {13}', server['name'], project, tr(STATUS.get(server['status'], server['status'])), (public.get('ipv4') or {}).get('ip', tr('ندارد')), (public.get('ipv6') or {}).get('ip', tr('ندارد')), location.get('city') or location.get('name', '—'), plan.get('name', '—'), plan.get('cores', '—'), plan.get('memory', '—'), server.get('disk', '—'), image.get('description') or image.get('name', '—'), gb(server.get('outgoing_traffic')), tr('فعال') if server.get('backup_window') else tr('غیرفعال'), server['id']))


class SimpleUI:
    def __init__(self, manager):
        self.manager = manager

    async def start(self, update, context, pid, operation, resource_id=None, image_id=None):
        _, token = await self.manager.project(update, pid)
        self.manager.reset(context)
        path, body, labels = {}, {}, {}
        server = None
        ip_location = None
        if resource_id is not None:
            path = {'id': int(resource_id)}
            if '/servers/' in BY_ID[operation]['path']:
                server = (await self.manager.cloud.execute(token, BY_ID['get_server'], {'path': path}))['server']
                labels[tr('سرور')] = f"{server['name']} (#{server['id']})"
                if operation == 'change_server_type' and server['status'] != 'off':
                    raise ValueError(tr('برای تغییر پلن، ابتدا سرور را خاموش کن و سپس دوباره این گزینه را بزن.'))
            elif '/primary_ips/' in BY_ID[operation]['path']:
                ip = (await self.manager.cloud.execute(token, BY_ID['get_primary_ip'], {'path': path}))['primary_ip']
                labels['IP'] = f"{ip['name']} · {ip.get('ip', '')}"
                ip_location = (ip.get('location') or (ip.get('datacenter') or {}).get('location') or {}).get('name')
            elif '/images/' in BY_ID[operation]['path']:
                image = (await self.manager.cloud.execute(token, BY_ID['get_image'], {'path': path}))['image']
                labels[tr('اسنپ\u200cشات')] = image.get('description') or image.get('name') or str(resource_id)
        steps = {'create_server': [('name', 'text', tr('برای سرور یک نام انگلیسی بنویس؛ مثلاً my-server.')), ('location', 'location', tr('سرور در کدام کشور / شهر ساخته شود؟')), ('server_type', 'plan', tr('یکی از پلن\u200cهای موجود در همین موقعیت را انتخاب کن.')), ('image', 'image', tr('سیستم\u200cعامل سرور را انتخاب کن.')), ('ssh_keys', 'ssh', tr('کلید SSH را انتخاب کن یا از این مرحله بگذر.'))], 'update_server': [('name', 'text', tr('نام جدید سرور را به انگلیسی بنویس.'))], 'create_server_image': [('description', 'text', tr('یک نام برای این اسنپ\u200cشات بنویس.'))], 'rebuild_server': [('image', 'restore', tr('سیستم\u200cعامل یا بکاپ برای نصب مجدد را انتخاب کن. همهٔ اطلاعات دیسک پاک می\u200cشود.'))], 'change_server_type': [('server_type', 'plan', tr('پلن جدید را انتخاب کن. سرور باید خاموش باشد.')), ('upgrade_disk', 'disk', tr('دیسک هم بزرگ شود؟ افزایش اندازهٔ دیسک برگشت\u200cپذیر نیست.'))], 'create_primary_ip': [('name', 'text', tr('برای IP یک نام انگلیسی بنویس.')), ('type', 'family', tr('نوع IP را انتخاب کن.')), ('location', 'ip_location', tr('موقعیت IP را انتخاب کن.'))], 'assign_primary_ip': [('assignee_id', 'server', tr('IP به کدام سرور متصل شود؟ سرور باید خاموش باشد.'))], 'update_primary_ip': [('name', 'text', tr('نام جدید IP را به انگلیسی بنویس.'))], 'update_image': [('description', 'text', tr('نام جدید اسنپ\u200cشات را بنویس.'))], 'attach_server_iso': [('iso', 'iso', tr('فایل ISO سازگار با سرور را انتخاب کن.'))], 'attach_server_to_network': [('network', 'network', tr('شبکهٔ خصوصی را انتخاب کن.'))], 'detach_server_from_network': [('network', 'network', tr('کدام شبکه از سرور جدا شود؟'))], 'change_server_dns_ptr': [('ip', 'text', tr('آدرس IP کامل را بنویس.')), ('dns_ptr', 'text', tr('نام Reverse DNS را بنویس.'))]}.get(operation, [])
        image_arch = None
        if image_id is not None:
            image = (await self.manager.cloud.execute(token, BY_ID['get_image'], {'path': {'id': int(image_id)}}))['image']
            body['image'] = str(image_id)
            labels['image'] = image.get('description') or str(image_id)
            image_arch = image.get('architecture')
            steps = []
        if operation in {'create_server_image', 'rebuild_server'} and resource_id is None:
            steps = [('target', 'server', tr('کدام سرور؟'))] + steps
        if operation == 'create_server_image':
            body['type'] = 'snapshot'
        if operation == 'enable_server_rescue':
            body['type'] = 'linux64'
        if operation in {'create_primary_ip', 'assign_primary_ip'}:
            body['assignee_type'] = 'server'
        nav = context.user_data.get('nav')
        if operation == 'create_primary_ip' and nav and nav['pid'] == pid:
            context_server = (await self.manager.cloud.execute(token, BY_ID['get_server'],
                              {'path': {'id': nav['server']}}))['server']
            location = (context_server.get('datacenter') or {}).get('location') or {}
            if location.get('name'):
                body['location'] = location['name']
                labels['location'] = location.get('city') or location['name']
                steps = [step for step in steps if step[0] != 'location']
        state = {'kind': 'simple', 'pid': pid, 'operation': operation, 'path': path, 'body': body,
                 'labels': labels, 'steps': steps, 'step': 0, 'page': 0,
                 'expires': time.monotonic() + 900, 'server': server, 'image_arch': image_arch,
                 'ip_location': ip_location}
        context.user_data['input'] = state
        await self.render(update, context)

    def state(self, context):
        state = context.user_data.get('input', {})
        if state.get('kind') != 'simple' or state['expires'] < time.monotonic():
            raise ValueError(tr('این مرحله منقضی شده؛ از منوی پروژه دوباره شروع کن.'))
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
                label = tr('{0} · {1} هسته · {2} GB RAM', plan['name'], plan.get('cores', '?'), plan.get('memory', '?'))
                if amount and currency:
                    label += tr(' · {0} {1}/ماه', amount, currency)
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
        if mode == 'iso':
            architecture = ((state.get('server') or {}).get('server_type') or {}).get('architecture')
            return [(str(x['id']), x.get('description') or x.get('name') or str(x['id']), x)
                    for x in await listing('isos') if x.get('architecture') in {None, architecture} and not x.get('deprecated')]
        if mode == 'network':
            attached = {x['network'] for x in (state.get('server') or {}).get('private_net', [])}
            return [(x['id'], x['name'], x) for x in await listing('networks')
                    if (x['id'] in attached) == (state['operation'] == 'detach_server_from_network')]
        if mode == 'datacenter':
            return [(x['name'], f"{x['location'].get('city', '')} · {x['name']}", x)
                    for x in await listing('datacenters')]
        if mode == 'server':
            return [(x['id'], f"{x['name']} · {tr(STATUS.get(x['status'], x['status']))}", x)
                    for x in await listing('servers') if x.get('status') != 'deleting'
                    and (state['operation'] != 'assign_primary_ip' or x.get('status') == 'off')
                    and (not state.get('ip_location') or ((x.get('datacenter') or {}).get('location') or {}).get('name') == state['ip_location'])
                    and (not state.get('image_arch') or (x.get('server_type') or {}).get('architecture') == state['image_arch'])]
        if mode == 'family':
            return [('ipv4', tr('IPv4 (دارای هزینه)'), {}), ('ipv6', 'IPv6', {})]
        if mode == 'disk':
            return [(False, tr('فقط CPU و RAM؛ دیسک تغییر نکند'), {}),
                    (True, tr('CPU و RAM و دیسک بزرگ\u200cتر شوند'), {})]
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
            question += tr('\nقیمت مربوط به پلن است؛ IP و سرویس\u200cهای اضافه هزینهٔ جدا دارند.')
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
                nav.append((tr('قبلی'), prefix + 'page:-1'))
            if (state['page']+1)*6 < len(options):
                nav.append((tr('بعدی'), prefix + 'page:1'))
            if nav:
                rows.append(nav)
            if not options:
                question += tr('\nدر حال حاضر گزینهٔ موجودی پیدا نشد. می\u200cتوانی به مرحلهٔ قبل برگردی.')
            if mode == 'ssh':
                rows.append([(tr('بدون SSH؛ دریافت گذرواژهٔ سرور'), prefix + 'skip')])
        if state['step']:
            rows.append([(tr('↩️ مرحلهٔ قبل'), prefix + 'back')])
        rows.append([(tr('✖️ انصراف'), f"p:{state['pid']}")])
        await self.manager.show(update, tr('{0}\nپروژه: {1}\nمرحلهٔ {2} از {3}\n\n{4}', title(BY_ID[state['operation']]), project, state['step'] + 1, len(state['steps']), question), rows)

    async def callback(self, update, context, parts):
        state = self.state(context)
        if not secrets.compare_digest(parts[1], state['nonce']):
            raise ValueError(tr('این دکمه مربوط به مرحلهٔ قبلی است؛ از آخرین پیام استفاده کن.'))
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
                    raise ValueError(tr('این مرحله قابل رد کردن نیست.'))
                state['body'].pop(key, None)
            else:
                value, label, metadata = state['options'][int(parts[3])]
                if mode == 'plan' and not available_plan(metadata, state['body'].get('location')):
                    raise ValueError(tr('این پلن دیگر موجود نیست؛ فهرست را تازه کن.'))
                if key == 'target':
                    state['path'] = {'id': value}
                    state['labels'][tr('سرور')] = label
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
            raise ValueError(tr('برای این مرحله یکی از دکمه\u200cها را انتخاب کن.'))
        op = BY_ID[state['operation']]
        schema = op['requestBody']['content']['application/json']['schema']['properties'][key]
        value = scalar(text.strip(), schema)
        if key == 'name' and not all(c.isascii() and (c.isalnum() or c in '-.') for c in value):
            raise ValueError(tr('نام باید انگلیسی و شامل حروف، عدد، خط تیره یا نقطه باشد.'))
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
            raise ValueError(tr('این پلن در موقعیت انتخاب\u200cشده موجود نیست. دوباره ساخت سرور را شروع کن.'))
        prepare(BY_ID['create_server'], payload)
