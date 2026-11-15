"""Schema-driven conversation forms; input stays bound to one user, project and operation."""
import copy
import math
import secrets
import time

from jsonschema import Draft202012Validator

from .api import BY_ID, CATALOG, OPERATIONS, prepare, schema_for
from .availability import all_items, available_plan

SOURCES = {
    "server": ("servers", "id"), "server_type": ("server_types", "name"),
    "image": ("images", "id"), "location": ("locations", "name"),
    "home_location": ("locations", "name"), "datacenter": ("datacenters", "name"),
    "ssh_keys": ("ssh_keys", "id"), "network": ("networks", "id"),
    "firewall": ("firewalls", "id"),
    "networks": ("networks", "id"), "placement_group": ("placement_groups", "id"),
    "iso": ("isos", "id"), "certificates": ("certificates", "id"),
    "primary_ipv4": ("primary_ips", "id"), "primary_ipv6": ("primary_ips", "id"),
    "load_balancer_type": ("load_balancer_types", "name"),
}

FIELDS = {
    "path": "شناسهٔ منبع", "body": "تنظیمات", "query": "فیلتر و صفحه‌بندی",
    "id": "شناسه", "id_or_name": "شناسه یا نام دامنه", "name": "نام", "type": "نوع",
    "description": "توضیحات", "server_type": "مدل سرور", "image": "ایمیج / بکاپ / اسنپ‌شات",
    "location": "موقعیت دیتاسنتر", "datacenter": "دیتاسنتر", "ssh_keys": "کلیدهای SSH",
    "public_key": "کلید عمومی SSH", "user_data": "اسکریپت راه‌اندازی Cloud-init",
    "start_after_create": "روشن شدن پس از ساخت", "automount": "اتصال خودکار دیسک",
    "size": "اندازه به گیگابایت", "format": "فرمت فایل‌سیستم", "server": "سرور",
    "network": "شبکه", "networks": "شبکه‌ها", "ip": "آدرس IP", "ip_range": "محدودهٔ IP با CIDR",
    "network_zone": "ناحیهٔ شبکه", "subnets": "زیرشبکه‌ها", "routes": "مسیرها",
    "destination": "مقصد", "gateway": "دروازه", "labels": "برچسب‌ها", "selector": "انتخابگر برچسب",
    "label_selector": "فیلتر برچسب", "rules": "قوانین", "direction": "جهت ترافیک",
    "protocol": "پروتکل", "port": "پورت / بازهٔ پورت", "source_ips": "IPهای مجاز مبدأ (CIDR)",
    "destination_ips": "IPهای مجاز مقصد (CIDR)", "apply_to": "اعمال روی منابع",
    "resources": "منابع", "firewalls": "فایروال‌ها", "public_net": "شبکهٔ عمومی",
    "enable_ipv4": "فعال کردن IPv4", "enable_ipv6": "فعال کردن IPv6",
    "primary_ipv4": "شناسهٔ IPv4 اصلی", "primary_ipv6": "شناسهٔ IPv6 اصلی",
    "delete": "محافظت در برابر حذف", "rebuild": "محافظت در برابر نصب مجدد",
    "upgrade_disk": "ارتقای دیسک همراه مدل سرور", "dns_ptr": "نام Reverse DNS",
    "placement_group": "گروه استقرار", "iso": "فایل ISO", "alias_ips": "IPهای مستعار",
    "home_location": "موقعیت اصلی", "assignee_id": "شناسهٔ سرور مقصد",
    "assignee_type": "نوع مقصد", "auto_delete": "حذف خودکار همراه سرور",
    "load_balancer_type": "مدل لودبالانسر", "algorithm": "الگوریتم توزیع",
    "targets": "مقصدها", "services": "سرویس‌ها", "listen_port": "پورت ورودی",
    "destination_port": "پورت مقصد", "health_check": "بررسی سلامت", "http": "تنظیمات HTTP",
    "domain": "دامنه", "domains": "دامنه‌ها", "certificate": "گواهی",
    "private_key": "کلید خصوصی", "certificates": "گواهی‌ها", "use_private_ip": "استفاده از IP خصوصی",
    "proxyprotocol": "پروتکل Proxy", "interval": "فاصلهٔ بررسی", "timeout": "مهلت",
    "retries": "تعداد تلاش", "ttl": "زمان کش TTL", "mode": "حالت",
    "records": "رکوردها", "value": "مقدار", "comment": "توضیح", "rr_name": "نام رکورد DNS",
    "rr_type": "نوع رکورد DNS", "zonefile": "محتوای فایل Zone", "page": "شمارهٔ صفحه",
    "per_page": "تعداد در صفحه", "sort": "مرتب‌سازی", "status": "وضعیت",
    "start": "زمان شروع", "end": "زمان پایان", "step": "گام زمانی",
    "primary_nameservers": "نیم‌سرورهای اصلی", "vswitch_id": "شناسهٔ vSwitch",
}

TITLES = {
    "create_server": "ساخت سرور", "update_server": "تغییر نام و برچسب سرور",
    "delete_server": "حذف سرور", "change_server_type": "تغییر مدل و منابع سرور",
    "rebuild_server": "نصب مجدد / بازیابی از ایمیج", "enable_server_backup": "فعال کردن بکاپ",
    "disable_server_backup": "غیرفعال کردن بکاپ", "enable_server_rescue": "فعال کردن Rescue",
    "disable_server_rescue": "غیرفعال کردن Rescue", "reset_server_password": "بازنشانی گذرواژه",
    "request_server_console": "دریافت اطلاعات کنسول", "reset_server": "ریست سخت‌افزاری",
    "poweron_server": "روشن کردن", "poweroff_server": "قطع برق", "shutdown_server": "خاموش کردن نرم",
    "reboot_server": "ری‌استارت", "create_server_image": "ساخت اسنپ‌شات / ایمیج",
    "change_server_protection": "محافظت حذف و نصب مجدد", "get_server_metrics": "آمار مصرف سرور",
    "attach_server_to_network": "اتصال سرور به شبکه", "detach_server_from_network": "قطع اتصال شبکه",
    "attach_server_iso": "اتصال ISO", "detach_server_iso": "جدا کردن ISO",
    "change_server_dns_ptr": "تنظیم Reverse DNS", "change_server_alias_ips": "تغییر IPهای مستعار",
    "create_network": "ساخت شبکه", "add_network_subnet": "افزودن زیرشبکه",
    "add_network_route": "افزودن مسیر", "create_firewall": "ساخت فایروال",
    "set_firewall_rules": "تنظیم قوانین فایروال", "apply_firewall_to_resources": "اعمال فایروال",
    "remove_firewall_from_resources": "حذف اتصال فایروال", "create_volume": "ساخت دیسک",
    "resize_volume": "افزایش اندازهٔ دیسک", "attach_volume": "اتصال دیسک", "detach_volume": "جدا کردن دیسک",
    "create_floating_ip": "ساخت Floating IP", "assign_floating_ip": "تخصیص Floating IP",
    "create_primary_ip": "ساخت Primary IP", "assign_primary_ip": "تخصیص Primary IP",
    "create_ssh_key": "افزودن کلید SSH", "create_load_balancer": "ساخت لودبالانسر",
    "create_certificate": "ساخت / وارد کردن گواهی", "create_placement_group": "ساخت گروه استقرار",
    "create_zone": "افزودن دامنهٔ DNS", "create_zone_rrset": "افزودن رکورد DNS",
    "set_zone_rrset_records": "تنظیم مقادیر رکورد", "change_zone_ttl": "تغییر TTL دامنه",
    "import_zone_zonefile": "وارد کردن فایل Zone", "get_pricing": "قیمت منابع",
}


def title(operation):
    if operation["id"] in TITLES:
        return TITLES[operation["id"]]
    nouns = {"server": "سرور", "servers": "سرورها", "network": "شبکه", "networks": "شبکه‌ها",
             "volume": "دیسک", "volumes": "دیسک‌ها", "firewall": "فایروال", "firewalls": "فایروال‌ها",
             "image": "ایمیج", "images": "ایمیج‌ها", "zone": "دامنه", "zones": "دامنه‌ها",
             "zone_rrset": "رکورد DNS", "zone_rrsets": "رکوردهای DNS",
             "floating_ip": "Floating IP", "floating_ips": "Floating IPها",
             "primary_ip": "Primary IP", "primary_ips": "Primary IPها",
             "ssh_key": "کلید SSH", "ssh_keys": "کلیدهای SSH",
             "certificate": "گواهی", "certificates": "گواهی‌ها",
             "placement_group": "گروه استقرار", "placement_groups": "گروه‌های استقرار",
             "load_balancer": "لودبالانسر", "load_balancers": "لودبالانسرها"}
    verb, _, noun = operation["id"].partition('_')
    verbs = {"get": "مشاهدهٔ", "list": "فهرست", "delete": "حذف", "update": "ویرایش",
             "create": "ساخت", "unassign": "برداشتن تخصیص"}
    if verb in verbs and noun in nouns:
        return verbs[verb] + ' ' + nouns[noun]
    return operation["summary"]


def resolve(schema):
    schema = copy.deepcopy(schema)
    if "$ref" in schema:
        reference = CATALOG
        for key in schema.pop("$ref").removeprefix("#/").split("/"):
            reference = reference[key]
        schema = {**resolve(reference), **schema}
    for part in schema.pop("allOf", []):
        part = resolve(part)
        schema["properties"] = {**schema.get("properties", {}), **part.get("properties", {})}
        schema["required"] = list(dict.fromkeys(schema.get("required", []) + part.get("required", [])))
        schema = {**part, **schema}
    return schema


def kind(schema):
    value = schema.get("type", "object")
    return next((x for x in value if x != "null"), "null") if isinstance(value, list) else value


def read(data, path):
    for part in path:
        data = data[part]
    return data


def write(data, path, value):
    parent = read(data, path[:-1])
    key = path[-1]
    if isinstance(parent, list) and key == len(parent):
        parent.append(value)
    else:
        parent[key] = value


def node_schema(state, path=None):
    path = state["cursor"] if path is None else path
    schema = schema_for(OPERATIONS[state["index"]])
    for depth in range(len(path) + 1):
        schema = resolve(schema)
        union = schema.get("oneOf", schema.get("anyOf"))
        if union:
            choice = state["variants"].get(tuple(path[:depth]))
            if choice is None:
                if depth == len(path):
                    return schema
                raise ValueError("ابتدا نوع مقدار را انتخاب کنید.")
            schema = resolve({**{k: v for k, v in schema.items() if k not in {"oneOf", "anyOf"}},
                              **union[choice]})
        if depth == len(path):
            return schema
        key = path[depth]
        if kind(schema) == "array":
            schema = schema["items"]
        else:
            schema = schema.get("properties", {}).get(key, schema.get("additionalProperties", {}))
            if schema is True:
                schema = {"type": "string"}
            if not isinstance(schema, dict):
                raise ValueError("این فیلد قابل ویرایش نیست.")


def scalar(text, schema):
    value_type = kind(schema)
    try:
        if value_type == "integer":
            value = int(text)
        elif value_type == "number":
            value = float(text)
            if not math.isfinite(value):
                raise ValueError
        elif value_type == "boolean":
            if text.lower() not in {"true", "false"}:
                raise ValueError
            value = text.lower() == "true"
        elif value_type == "null":
            value = None
        else:
            value = text
    except ValueError:
        raise ValueError("نوع مقدار درست نیست؛ مطابق راهنمای فیلد وارد کنید.") from None
    validate(value, schema)
    return value


def validate(value, schema):
    schema = {**schema, "components": CATALOG["components"]}
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error:
        if error.validator == "required" and isinstance(value, dict):
            missing = [FIELDS.get(x, x) for x in error.validator_value if x not in value]
            raise ValueError("فیلدهای الزامی را تکمیل کنید: " + "، ".join(missing))
        raise ValueError(f"مقدار با قانون {error.validator} سازگار نیست؛ راهنمای فیلد را بررسی کنید.")


def source_for(cursor):
    name = next((x for x in reversed(cursor) if isinstance(x, str)), "")
    if name == "id" and "server" in cursor:
        return SOURCES["server"]
    return SOURCES.get(name)


class Forms:
    def __init__(self, manager):
        self.manager = manager

    async def start(self, update, context, pid, index, path=None):
        await self.manager.project(update, pid)
        context.user_data.clear()
        payload = {"path": path} if path else {}
        state = {"kind": "form", "pid": pid, "index": index, "payload": payload,
                 "cursor": [], "variants": {}, "expires": time.monotonic() + 900, "page": 0}
        context.user_data["input"] = state
        await self.render(update, context)

    def state(self, context):
        state = context.user_data.get("input", {})
        if state.get("kind") != "form" or state["expires"] < time.monotonic():
            raise ValueError("فرم منقضی شده؛ عملیات را دوباره انتخاب کنید.")
        return state

    def accepts_document(self, context):
        state = self.state(context)
        return state.get('waiting') == 'scalar' and kind(node_schema(state)) == 'string'

    async def render(self, update, context):
        keyboard = self.manager.keyboard
        state = self.state(context)
        name, _ = await self.manager.project(update, state["pid"])
        schema = node_schema(state)
        cursor = state["cursor"]
        state["nonce"] = secrets.token_hex(8)
        state["waiting"] = None
        prefix = f"flow:{state['nonce']}:"
        rows = []
        union = schema.get("oneOf", schema.get("anyOf"))
        heading = f"پروژه: {name}\n{title(OPERATIONS[state['index']])}\n"
        heading += " ← ".join(FIELDS.get(str(x), str(x)) for x in cursor) or "فرم عملیات"
        if union:
            rows = [[(f"گزینه {i+1}: {part.get('title', kind(resolve(part)))}", prefix + f"variant:{i}")]
                    for i, part in enumerate(union)]
            heading += "\nنوع ورودی را انتخاب کنید."
        elif kind(schema) in {"object", "array"}:
            try:
                current = read(state["payload"], cursor)
            except (KeyError, IndexError):
                current = None
            if (kind(schema) == "object" and not isinstance(current, dict)
                    or kind(schema) == "array" and not isinstance(current, list)):
                current = {} if kind(schema) == "object" else []
                write(state["payload"], cursor, current)
            if kind(schema) == "object":
                keys = list(schema.get("properties", {}))
                # Empty path/query groups have no useful fields and need not be displayed.
                if not cursor:
                    keys = [k for k in keys if schema["properties"][k].get("properties")
                            or k == "body"]
                keys += [k for k in current if k not in keys]
                state["keys"] = keys
                for i in range(state["page"]*8, min(len(keys), (state["page"]+1)*8)):
                    key = keys[i]
                    status = "✓" if key in current else ("*" if key in schema.get("required", []) else "○")
                    rows.append([(f"{status} {FIELDS.get(key, key)}", prefix + f"field:{i}")])
                if isinstance(schema.get("additionalProperties"), dict) or schema.get("additionalProperties") is True:
                    rows.append([("➕ کلید دلخواه", prefix + "map")])
                heading += "\n* الزامی · ○ اختیاری · ✓ واردشده؛ فیلد را انتخاب کنید."
            else:
                keys = list(range(len(current)))
                state["keys"] = keys
                for i in keys[state["page"]*8:(state["page"]+1)*8]:
                    rows.append([(f"ویرایش مورد {i+1}", prefix + f"field:{i}")])
                rows.append([("➕ افزودن مورد", prefix + "append")])
                heading += f"\nتعداد موارد: {len(current)}"
            nav = []
            if state["page"]:
                nav.append(("قبلی", prefix + "page:-1"))
            if (state["page"]+1)*8 < len(keys):
                nav.append(("بعدی", prefix + "page:1"))
            if nav:
                rows.append(nav)
            rows.append([("ثبت این بخش / بازگشت" if cursor else "✅ بررسی و ادامه", prefix + "done")])
        else:
            options = schema.get("enum")
            if options is None and kind(schema) == "boolean":
                options = [True, False]
            state["options"] = options or []
            if options:
                rows += [[(str(value), prefix + f"value:{i}")] for i, value in enumerate(options)]
                heading += "\nمقدار را انتخاب کنید یا تایپ کنید."
            else:
                heading += f"\nمقدار را بفرستید. نوع: {kind(schema)}"
                if kind(schema) == 'string':
                    heading += "\nبرای متن طولانی می‌توانید فایل UTF-8 بفرستید."
            if source_for(cursor):
                rows.append([("📋 انتخاب از فهرست واقعی پروژه", prefix + "pick:1")])
            description = schema.get("description", "")[:700]
            if description:
                heading += "\nراهنمای هتزنر: " + description
            state["waiting"] = "scalar"
        if cursor:
            rows.append([("حذف این مقدار / صرف‌نظر", prefix + "remove"), ("بازگشت", prefix + "back")])
        rows.append([("لغو", "cancel")])
        await update.effective_message.reply_text(heading[:3500], reply_markup=keyboard(rows))

    async def picker(self, update, context, page):
        keyboard = self.manager.keyboard
        state = self.state(context)
        collection, value_key = source_for(state["cursor"])
        name, token = await self.manager.project(update, state["pid"])
        if collection == 'server_types':
            plans = await all_items(self.manager.cloud, token, BY_ID['list_server_types'], 'server_types')
            location = state['payload'].get('body', {}).get('location')
            available = [p for p in plans if available_plan(p, location)]
            items = available[(page-1)*8:page*8]
            has_next = page*8 < len(available)
        else:
            result = await self.manager.cloud.execute(token, BY_ID['list_' + collection],
                                                       {"query": {"page": page, "per_page": 8}})
            items = result[collection]
            has_next = result.get('meta', {}).get('pagination', {}).get('next_page')
        state["nonce"] = secrets.token_hex(8)
        state["pick_values"] = [item[value_key] for item in items]
        prefix = f"flow:{state['nonce']}:"
        rows = []
        for i, item in enumerate(items):
            label = item.get('name') or item.get('description') or str(item['id'])
            if collection == 'server_types':
                label += f" · {item.get('cores', '?')} CPU / {item.get('memory', '?')} GB"
            rows.append([(label, prefix + f"choose:{i}")])
        nav = []
        if page > 1:
            nav.append(("قبلی", prefix + f"pick:{page-1}"))
        if has_next:
            nav.append(("بعدی", prefix + f"pick:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([("ورود دستی / بازگشت به فیلد", prefix + "manual")])
        await update.effective_message.reply_text(
            f"پروژه: {name}\nانتخاب {FIELDS.get(str(state['cursor'][-1]), collection)} — صفحه {page}\n"
            "سازگاری مدل، موقعیت و معماری و هزینهٔ منبع را پیش از تأیید بررسی کنید.", reply_markup=keyboard(rows))

    async def callback(self, update, context, parts):
        state = self.state(context)
        if not secrets.compare_digest(parts[1], state["nonce"]):
            raise ValueError("این دکمه قدیمی است؛ از آخرین پیام فرم استفاده کنید.")
        await self.manager.project(update, state["pid"])
        action = parts[2]
        cursor = state["cursor"]
        schema = node_schema(state)
        if action == "pick":
            await self.picker(update, context, int(parts[3]))
            return
        if action == "choose":
            value = state["pick_values"][int(parts[3])]
            # A union may offer a string branch even though the API resource ID is an integer.
            if kind(schema) == "string":
                value = str(value)
            validate(value, schema)
            write(state["payload"], cursor, value)
            cursor.pop()
            state["page"] = 0
        elif action == "manual":
            pass
        elif action == "field":
            key = state["keys"][int(parts[3])]
            cursor.append(key)
            state["page"] = 0
        elif action == "append":
            cursor.append(len(read(state["payload"], cursor)))
            state["page"] = 0
        elif action == "variant":
            state["variants"][tuple(cursor)] = int(parts[3])
            # Switching variants starts a fresh value; incompatible old fields must not survive.
            if cursor:
                parent = read(state["payload"], cursor[:-1])
                if isinstance(parent, dict):
                    parent.pop(cursor[-1], None)
                elif cursor[-1] < len(parent):
                    parent[cursor[-1]] = None
            state["page"] = 0
        elif action == "value":
            value = state["options"][int(parts[3])]
            validate(value, schema)
            write(state["payload"], cursor, value)
            cursor.pop()
            state["page"] = 0
        elif action == "map":
            state["waiting"] = "map"
            await update.effective_message.reply_text("نام کلید جدید را بفرستید (مثلاً environment).")
            return
        elif action == "page":
            state["page"] = max(0, state["page"] + int(parts[3]))
        elif action in {"back", "remove"}:
            if action == "remove":
                parent = read(state["payload"], cursor[:-1])
                if isinstance(parent, dict):
                    parent.pop(cursor[-1], None)
                elif cursor[-1] < len(parent):
                    parent.pop(cursor[-1])
                # Array indices may have shifted; forget union selections under this container.
                state["variants"] = {k: v for k, v in state["variants"].items()
                                     if k[:len(cursor)-1] != tuple(cursor[:-1])}
            cursor.pop()
            state["page"] = 0
        elif action == "done":
            if cursor:
                validate(read(state["payload"], cursor), schema)
                cursor.pop()
                state["page"] = 0
            else:
                op = OPERATIONS[state["index"]]
                prepare(op, state["payload"])
                if op["method"] == "GET":
                    payload = state["payload"]
                    context.user_data.clear()
                    await self.manager.execute(update, context, state["pid"], op, payload)
                else:
                    await self.manager.preview(update, context, state["pid"], op, state["payload"])
                return
        await self.render(update, context)

    async def text(self, update, context, text):
        state = self.state(context)
        await self.manager.project(update, state["pid"])
        if state["waiting"] == "map":
            if not text or len(text) > 255:
                raise ValueError("نام کلید باید بین ۱ تا ۲۵۵ حرف باشد.")
            state["cursor"].append(text)
            await self.render(update, context)
            return
        if state["waiting"] != "scalar":
            raise ValueError("ابتدا فیلد مورد نظر را با دکمه انتخاب کنید.")
        schema = node_schema(state)
        value = scalar(text, schema)
        write(state["payload"], state["cursor"], value)
        state["cursor"].pop()
        state["page"] = 0
        await self.render(update, context)
