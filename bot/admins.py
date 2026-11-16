"""Owner-only delegation UI. Grant changes are committed together after explicit Save."""
import asyncio
import secrets
import sqlite3
import time

from .i18n import tr


class AdminUI:
    def __init__(self, manager):
        self.manager = manager

    def owner(self, update):
        self.manager.require_owner(update)
        return update.effective_user.id

    async def refresh_roles(self):
        self.manager.delegates = await asyncio.to_thread(self.manager.store.admin_ids, self.manager.allowed)

    async def get_admin(self, owner, admin):
        items = await asyncio.to_thread(self.manager.store.list_admins, owner)
        for uid, name in items:
            if uid == admin:
                return name
        raise ValueError(tr('ادمین پیدا نشد یا اجازهٔ مدیریت آن را ندارید.'))

    async def menu(self, update, context, page=0):
        owner = self.owner(update)
        context.user_data.clear()
        items = await asyncio.to_thread(self.manager.store.list_admins, owner)
        rows = [[(f'{name} · {uid}', f'admin:{uid}')]
                for uid, name in items[page*8:(page+1)*8]]
        nav = []
        if page:
            nav.append((tr('قبلی'), f'admins:{page-1}'))
        if (page+1)*8 < len(items):
            nav.append((tr('بعدی'), f'admins:{page+1}'))
        if nav:
            rows.append(nav)
        rows += [[(tr('➕ افزودن ادمین'), 'adminadd')], [(tr('بازگشت'), 'projects')]]
        await self.manager.show(update, tr('👥 مدیریت ادمین‌ها\nادمین را انتخاب کنید یا یک ادمین جدید بسازید.'), rows)

    async def detail(self, update, context, admin):
        owner = self.owner(update)
        name = await self.get_admin(owner, admin)
        context.user_data.clear()
        owned = await asyncio.to_thread(self.manager.store.owned_projects, owner)
        selected = await asyncio.to_thread(self.manager.store.assigned_projects, owner, admin)
        names = [name for pid, name in owned if pid in selected]
        await self.manager.show(update, tr('ادمین: {0}\nشناسهٔ تلگرام: {1}\nتعداد پروژه‌های اختصاص‌یافته: {2}', name, admin, len(names))
                                + '\n\n' + '\n'.join(names[:25]), [
            [(tr('📁 انتخاب پروژه‌ها'), f'admingrants:{admin}')],
            [(tr('🗑 حذف ادمین'), f'adminremove:{admin}')], [(tr('بازگشت'), 'admins:0')]])

    async def edit_grants(self, update, context, admin):
        owner = self.owner(update)
        await self.get_admin(owner, admin)
        options = await asyncio.to_thread(self.manager.store.owned_projects, owner)
        selected = await asyncio.to_thread(self.manager.store.assigned_projects, owner, admin)
        context.user_data.clear()
        context.user_data['input'] = {'kind': 'admin_grants', 'owner': owner, 'admin': admin, 'options': options,
                                      'selected': selected, 'page': 0, 'expires': time.monotonic() + 600}
        await self.render_grants(update, context)

    async def render_grants(self, update, context):
        state = self.state(update, context, 'admin_grants')
        name = await self.get_admin(state['owner'], state['admin'])
        state['nonce'] = secrets.token_hex(8)
        prefix = f"adminpick:{state['nonce']}:"
        rows = [[(f"{'✅' if pid in state['selected'] else '⬜'} {project}", prefix + f'toggle:{i}')]
                for i, (pid, project) in enumerate(state['options']) if state['page']*8 <= i < (state['page']+1)*8]
        nav = []
        if state['page']:
            nav.append((tr('قبلی'), prefix + 'page:-1'))
        if (state['page']+1)*8 < len(state['options']):
            nav.append((tr('بعدی'), prefix + 'page:1'))
        if nav:
            rows.append(nav)
        rows += [[(tr('💾 ذخیرهٔ دسترسی‌ها'), prefix + 'save')],
                 [(tr('✖️ انصراف'), f"admin:{state['admin']}")]]
        await self.manager.show(update, tr('پروژه‌های مجاز برای {0} را انتخاب کنید.\nادمین روی پروژه‌های انتخاب‌شده کنترل کامل عملیاتی دارد؛ از جمله ساخت و حذف منابع و ایجاد هزینه.\nتغییرات فقط با «ذخیرهٔ دسترسی‌ها» اعمال می‌شوند.', name), rows)

    def state(self, update, context, expected):
        owner = self.owner(update)
        state = context.user_data.get('input', {})
        if state.get('kind') != expected or state.get('owner') != owner or state['expires'] < time.monotonic():
            raise ValueError(tr('این مرحله منقضی شده؛ دوباره شروع کنید.'))
        return state

    async def callback(self, update, context, parts):
        owner = self.owner(update)
        kind = parts[0]
        if kind == 'admins':
            await self.menu(update, context, max(0, int(parts[1])))
        elif kind == 'admin':
            await self.detail(update, context, int(parts[1]))
        elif kind == 'adminadd':
            context.user_data.clear()
            context.user_data['input'] = {'kind': 'admin_id', 'owner': owner, 'expires': time.monotonic() + 600}
            await self.manager.show(update, tr('شناسهٔ عددی تلگرام ادمین را بفرستید. نام کاربری یا شمارهٔ تلفن پذیرفته نیست.'),
                                    [[(tr('✖️ انصراف'), 'admins:0')]])
        elif kind == 'admingrants':
            await self.edit_grants(update, context, int(parts[1]))
        elif kind == 'adminpick':
            state = self.state(update, context, 'admin_grants')
            if not secrets.compare_digest(state['nonce'], parts[1]):
                raise ValueError(tr('این دکمه قدیمی است؛ از آخرین پیام استفاده کنید.'))
            if parts[2] == 'page':
                state['page'] = max(0, state['page'] + int(parts[3]))
            elif parts[2] == 'toggle':
                pid = state['options'][int(parts[3])][0]
                state['selected'].symmetric_difference_update({pid})
            elif parts[2] == 'save':
                await asyncio.to_thread(self.manager.store.replace_grants, owner, state['admin'], state['selected'])
                await self.detail(update, context, state['admin'])
                return
            await self.render_grants(update, context)
        elif kind == 'adminremove':
            admin = int(parts[1])
            name = await self.get_admin(owner, admin)
            nonce = secrets.token_hex(12)
            context.user_data.clear()
            context.user_data['input'] = {'kind': 'admin_remove', 'owner': owner, 'admin': admin,
                                          'nonce': nonce, 'expires': time.monotonic() + 120}
            await self.manager.show(update, tr('ادمین {0} و تمام دسترسی‌های داده‌شده توسط شما حذف شود؟', name), [
                [(tr('✅ تأیید و انجام'), f'adminconfirm:{nonce}'), (tr('✖️ انصراف'), f'admin:{admin}')]])
        elif kind == 'adminconfirm':
            state = self.state(update, context, 'admin_remove')
            if not secrets.compare_digest(state['nonce'], parts[1]):
                raise ValueError(tr('این دکمه قدیمی است؛ از آخرین پیام استفاده کنید.'))
            await asyncio.to_thread(self.manager.store.remove_admin, owner, state['admin'])
            await self.refresh_roles()
            await self.menu(update, context)

    async def text(self, update, context, text):
        owner = self.owner(update)
        kind = context.user_data.get('input', {}).get('kind')
        state = self.state(update, context, kind)
        if kind == 'admin_id':
            try:
                admin = int(text)
            except ValueError:
                raise ValueError(tr('شناسه یا نام ادمین معتبر نیست.')) from None
            if not 0 < admin <= 2**63-1 or admin in self.manager.allowed:
                raise ValueError(tr('این شناسه معتبر نیست یا از قبل مالک بات است.'))
            if admin in dict(await asyncio.to_thread(self.manager.store.list_admins, owner)):
                raise ValueError(tr('این ادمین قبلاً ثبت شده است؛ از فهرست ادمین‌ها انتخاب کنید.'))
            state.update(kind='admin_name', admin=admin)
            await update.effective_message.reply_text(tr('یک نام دلخواه برای ادمین بنویسید.'))
        elif kind == 'admin_name':
            if not text.strip() or len(text) > 60 or '\n' in text or '\r' in text:
                raise ValueError(tr('شناسه یا نام ادمین معتبر نیست.'))
            try:
                await asyncio.to_thread(self.manager.store.add_admin, owner, state['admin'], text)
            except sqlite3.IntegrityError:
                raise ValueError(tr('این ادمین قبلاً ثبت شده است؛ از فهرست ادمین‌ها انتخاب کنید.')) from None
            await self.refresh_roles()
            await self.edit_grants(update, context, state['admin'])
        else:
            raise ValueError(tr('ابتدا از دکمه‌های مدیریت ادمین استفاده کنید.'))
