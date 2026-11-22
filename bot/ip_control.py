"""Primary IP changes are journalled before unassignment and reconciled after failure/restart."""
from .i18n import tr
import asyncio
import sqlite3

from .api import BY_ID, CloudError
from .availability import location_name

UNSET = object()


class IPControl:
    def __init__(self, store, cloud):
        self.store, self.cloud = store, cloud

    async def server(self, token, server_id):
        return (await self.cloud.execute(token, BY_ID['get_server'], {'path': {'id': server_id}}))['server']

    async def ip(self, token, ip_id):
        return (await self.cloud.execute(token, BY_ID['get_primary_ip'], {'path': {'id': ip_id}}))['primary_ip']

    async def action(self, token, operation, ip_id, server_id=None):
        payload = {'path': {'id': ip_id}}
        if server_id is not None:
            payload['body'] = {'assignee_type': 'server', 'assignee_id': server_id}
        result = await self.cloud.execute(token, BY_ID[operation], payload)
        action = result.get('action', {})
        for _ in range(60):
            if action.get('status') == 'success':
                return
            if action.get('status') == 'error':
                raise CloudError(tr('تغییر IP در هتزنر ناموفق بود.'))
            if not action.get('id'):
                raise CloudError(tr('وضعیت تغییر IP نامشخص است؛ دوباره اجرا نکنید.'))
            await asyncio.sleep(1)
            action = (await self.cloud.execute(token, BY_ID['get_action'], {'path': {'id': action['id']}}))['action']
        raise CloudError(tr('مهلت پیگیری IP تمام شد؛ وضعیت سرور را بررسی کنید.'))

    async def server_action(self, token, operation, server_id):
        result = await self.cloud.execute(token, BY_ID[operation], {'path': {'id': server_id}})
        action = result.get('action', {})
        for _ in range(60):
            if action.get('status') == 'success':
                expected = 'off' if operation == 'shutdown_server' else 'running'
                for _ in range(60):
                    server = await self.server(token, server_id)
                    if server.get('status') == expected and server.get('locked') is False:
                        return
                    await asyncio.sleep(1)
                raise CloudError(tr('مهلت تغییر وضعیت سرور تمام شد؛ وضعیت IP را بررسی کنید.'))
            if action.get('status') == 'error':
                raise CloudError(tr('تغییر وضعیت سرور در هتزنر ناموفق بود.'))
            if not action.get('id'):
                raise CloudError(tr('وضعیت تغییر وضعیت سرور نامشخص است؛ سرور را بررسی کنید.'))
            await asyncio.sleep(1)
            action = (await self.cloud.execute(token, BY_ID['get_action'],
                                               {'path': {'id': action['id']}}))['action']
        raise CloudError(tr('مهلت پیگیری تغییر وضعیت سرور تمام شد؛ سرور را بررسی کنید.'))

    async def recover(self, row):
        owner, project, server_id, family, old_id, new_id = row[:6]
        server_was_running = bool(row[6]) if len(row) > 6 else False
        # Recovery is cleanup of an already-confirmed operation; use the project owner
        # even if the initiating delegate has since been revoked.
        principal = await asyncio.to_thread(self.store.project_owner, project)
        _, token = await asyncio.to_thread(self.store.get, principal, project)
        server = await self.server(token, server_id)
        if server.get('locked') is not False:
            return False
        current = (server.get('public_net', {}).get(family) or {}).get('id')
        if current == new_id:
            if server_was_running and server['status'] != 'running':
                if server['status'] != 'off':
                    return False
                await self.server_action(token, 'poweron_server', server_id)
            await asyncio.to_thread(self.store.finish_ip_change, project, server_id)
            return 'new'
        if current == old_id:
            if current is None and new_id is not None and (await self.ip(token, new_id)).get('assignee_id') == server_id:
                return False
            if server_was_running and server['status'] == 'off':
                await self.server_action(token, 'poweron_server', server_id)
            elif server_was_running and server['status'] != 'running':
                return False
            await asyncio.to_thread(self.store.finish_ip_change, project, server_id)
            return 'old'
        if server['status'] != 'off':
            return False
        if current is not None:
            return False  # An external change must never be overwritten.
        if new_id is not None:
            new = await self.ip(token, new_id)
            if new.get('assignee_id') == server_id:
                return False
        if old_id is not None:
            old = await self.ip(token, old_id)
            if old.get('assignee_id') is not None:
                return False
            await self.action(token, 'assign_primary_ip', old_id, server_id)
            current = ((await self.server(token, server_id)).get('public_net', {}).get(family) or {}).get('id')
            if current != old_id:
                return False
        if server_was_running:
            await self.server_action(token, 'poweron_server', server_id)
        await asyncio.to_thread(self.store.finish_ip_change, project, server_id)
        return 'old'

    async def change(self, owner, project, server_id, family, new_id, expected_old=UNSET, auto_restart=False):
        if family not in {'ipv4', 'ipv6'}:
            raise ValueError('Invalid IP family')
        _, token = await asyncio.to_thread(self.store.get, owner, project)
        server = await self.server(token, server_id)
        server_was_running = server.get('status') == 'running'
        if (server.get('locked') is not False
                or (auto_restart and (not server_was_running or new_id is None))
                or (not auto_restart and server['status'] != 'off')):
            raise ValueError(tr('برای تعویض خودکار IP، سرور باید روشن و برای تغییر دستی IP باید خاموش باشد.'))
        old_id = (server.get('public_net', {}).get(family) or {}).get('id')
        if expected_old is not UNSET and old_id != expected_old:
            raise ValueError(tr('IP فعلی سرور تغییر کرده است؛ دوباره انتخاب و تأیید کنید.'))
        if old_id == new_id:
            raise ValueError(tr('IP انتخاب\u200cشده تغییری ایجاد نمی\u200cکند.'))
        if old_id is not None:
            old = await self.ip(token, old_id)
            if old.get('assignee_id') != server_id or old['type'] != family:
                raise ValueError(tr('IP فعلی سرور تغییر کرده است؛ دوباره انتخاب و تأیید کنید.'))
        if new_id is not None:
            new = await self.ip(token, new_id)
            location = location_name(server)
            ip_location = location_name(new)
            if new['type'] != family or new.get('assignee_id') is not None or not location or ip_location != location:
                raise ValueError(tr('IP باید آزاد، هم\u200cنوع و در موقعیت همین سرور باشد.'))
        row = (owner, project, server_id, family, old_id, new_id, server_was_running if auto_restart else False)
        try:
            await asyncio.to_thread(self.store.begin_ip_change, *row)
        except sqlite3.IntegrityError:
            raise ValueError(tr('یک تغییر IP ناتمام برای این سرور وجود دارد؛ ابتدا بازیابی را بررسی کنید.')) from None
        try:
            if auto_restart:
                await self.server_action(token, 'shutdown_server', server_id)
            if old_id is not None:
                await self.action(token, 'unassign_primary_ip', old_id)
            if new_id is not None:
                await self.action(token, 'assign_primary_ip', new_id, server_id)
            current = ((await self.server(token, server_id)).get('public_net', {}).get(family) or {}).get('id')
            if current != new_id:
                raise CloudError(tr('وضعیت نهایی IP تأیید نشد.'))
            if auto_restart:
                await self.server_action(token, 'poweron_server', server_id)
            await asyncio.to_thread(self.store.finish_ip_change, project, server_id)
        except Exception:
            try:
                restored = await self.recover(row)
            except Exception:
                restored = False
            if restored:
                if restored == 'new':
                    return
                raise CloudError(tr('تغییر IP کامل نشد؛ وضعیت قبلی بازیابی شد. سرور را بررسی کنید.')) from None
            raise CloudError(tr('تغییر IP ناتمام است؛ وضعیت سرور و IP را بررسی کنید و دکمهٔ بازیابی را بزنید.')) from None
