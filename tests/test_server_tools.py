import ast
import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from string import Formatter
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from bot.api import BY_ID, CloudError, prepare
from bot.graphs import render_chart
from bot.i18n import ENGLISH, LANGUAGE, tr
from bot.ip_control import IPControl
from bot.main import Manager
from bot.storage import Store
from test_bot import update


class IPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, Fernet.generate_key().decode())
        self.pid = self.store.add(100, 'project', 'secret-token')
        self.current = 10
        self.off = True
        self.fail_new = False
        self.fail_rollback = False
        self.locked = False
        self.fail_verification = False
        self.requests = []
        self.ips = {10: {'id': 10, 'name': 'old', 'ip': '198.51.100.1', 'type': 'ipv4',
                         'assignee_id': 42, 'location': {'name': 'fsn1'}},
                    11: {'id': 11, 'name': 'new', 'ip': '198.51.100.2', 'type': 'ipv4',
                         'assignee_id': None, 'location': {'name': 'fsn1'}}}
        self.cloud = SimpleNamespace(execute=AsyncMock(side_effect=self.execute))
        self.control = IPControl(self.store, self.cloud)
        self.manager = Manager(self.store, self.cloud, {100, 200})
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def execute(self, token, operation, payload):
        self.assertEqual(token, 'secret-token')
        prepare(operation, payload)
        op = operation['id']
        self.requests.append((op, payload))
        if op == 'get_server':
            if self.fail_verification and self.current == 11:
                self.fail_verification = False
                raise CloudError('connection failure')
            ip = {'id': self.current, 'ip': '198.51.100.1'} if self.current is not None else None
            return {'server': {'id': 42, 'name': 'web', 'status': 'off' if self.off else 'running',
                               'locked': self.locked, 'public_net': {'ipv4': ip, 'ipv6': None},
                               'datacenter': {'location': {'name': 'fsn1'}}}}
        if op == 'get_primary_ip':
            return {'primary_ip': copy.deepcopy(self.ips[payload['path']['id']])}
        if op == 'list_primary_ips':
            return {'primary_ips': list(copy.deepcopy(self.ips).values())}
        if op == 'unassign_primary_ip':
            self.assertTrue(self.store.ip_changes(), 'Write journal before mutation')
            self.current = None
            self.ips[payload['path']['id']]['assignee_id'] = None
        if op == 'assign_primary_ip':
            ip_id = payload['path']['id']
            if (ip_id == 11 and self.fail_new) or (ip_id == 10 and self.fail_rollback):
                raise CloudError('assignment failed')
            self.current = ip_id
            self.ips[ip_id]['assignee_id'] = 42
        return {'action': {'id': 7, 'status': 'success'}}

    async def test_swap_is_ordered_and_journalled(self):
        await self.control.change(100, self.pid, 42, 'ipv4', 11, 10)
        mutations = [op for op, _ in self.requests if op in {'assign_primary_ip', 'unassign_primary_ip'}]
        self.assertEqual(mutations, ['unassign_primary_ip', 'assign_primary_ip'])
        self.assertEqual(self.current, 11)
        self.assertEqual(self.store.ip_changes(), [])

    async def test_unassign_keeps_previous_ip_and_creates_no_new_one(self):
        await self.control.change(100, self.pid, 42, 'ipv4', None, 10)
        self.assertIsNone(self.current)
        self.assertIn(10, self.ips)
        self.assertNotIn('delete_primary_ip', [op for op, _ in self.requests])

    async def test_failed_assignment_restores_previous_ip(self):
        self.fail_new = True
        with self.assertRaises(CloudError):
            await self.control.change(100, self.pid, 42, 'ipv4', 11, 10)
        self.assertEqual(self.current, 10)
        self.assertEqual(self.store.ip_changes(), [])

    async def test_failed_rollback_keeps_recoverable_journal(self):
        self.fail_new = self.fail_rollback = True
        with self.assertRaises(CloudError):
            await self.control.change(100, self.pid, 42, 'ipv4', 11, 10)
        self.assertIsNone(self.current)
        row = self.store.ip_changes()[0]
        self.fail_rollback = False
        self.assertEqual(await self.control.recover(row), 'old')
        self.assertEqual(self.current, 10)
        self.assertEqual(self.store.ip_changes(), [])

    async def test_connection_failure_after_success_does_not_undo_success(self):
        self.fail_verification = True
        await self.control.change(100, self.pid, 42, 'ipv4', 11, 10)
        self.assertEqual(self.current, 11)
        self.assertEqual(self.store.ip_changes(), [])

    async def test_powered_on_or_stale_confirmation_never_mutates(self):
        self.off = False
        with self.assertRaises(ValueError):
            await self.control.change(100, self.pid, 42, 'ipv4', 11, 10)
        self.off = True
        with self.assertRaises(ValueError):
            await self.control.change(100, self.pid, 42, 'ipv4', 11, 99)
        self.assertFalse(any(op in {'assign_primary_ip', 'unassign_primary_ip'} for op, _ in self.requests))

    async def test_incompatible_and_assigned_targets_rejected(self):
        for key, value in [('type', 'ipv6'), ('assignee_id', 999), ('location', {'name': 'hel1'})]:
            saved = copy.deepcopy(self.ips[11])
            self.ips[11][key] = value
            with self.assertRaises(ValueError):
                await self.control.change(100, self.pid, 42, 'ipv4', 11, 10)
            self.ips[11] = saved
        self.assertEqual(self.store.ip_changes(), [])

    async def test_external_change_during_recovery_is_never_overwritten(self):
        self.store.begin_ip_change(100, self.pid, 42, 'ipv4', 10, 11)
        self.current = 77
        self.assertFalse(await self.control.recover(self.store.ip_changes()[0]))
        self.assertTrue(self.store.ip_changes())

    async def test_project_with_pending_change_cannot_be_forgotten(self):
        self.store.begin_ip_change(100, self.pid, 42, 'ipv4', 10, 11)
        with self.assertRaises(ValueError):
            self.store.remove(100, self.pid)

    async def test_swap_menu_filters_targets_and_does_not_mutate_before_confirmation(self):
        self.ips[12] = {**self.ips[11], 'id': 12, 'type': 'ipv6'}
        self.ips[13] = {**self.ips[11], 'id': 13, 'location': {'name': 'hel1'}}
        await self.manager.ip_pick(update(), self.context, self.pid, 42, 'ipv4')
        state = self.context.user_data['input']
        self.assertEqual([x['id'] for x in state['options']], [11])
        event = update(callback=f"choice:{state['nonce']}:pick:0")
        await self.manager.callback(event, self.context)
        pending = self.context.user_data['pending']
        self.assertEqual(pending.payload, {'server': 42, 'family': 'ipv4', 'new_ip': 11, 'old_ip': 10})
        self.assertEqual(self.current, 10)
        await self.manager.callback(update(callback=f'yes:{pending.nonce}'), self.context)
        self.assertEqual(self.current, 11)

    async def test_owner_isolation(self):
        with self.assertRaises(ValueError):
            await self.control.change(200, self.pid, 42, 'ipv4', 11)
        self.cloud.execute.assert_not_awaited()

    async def test_cancellation_keeps_a_journal_for_recovery_after_restart(self):
        async def interrupted_action(token, operation, ip_id, server_id=None):
            if operation == 'unassign_primary_ip':
                self.current = None
                self.ips[10]['assignee_id'] = None
                return
            raise asyncio.CancelledError
        with patch.object(self.control, 'action', side_effect=interrupted_action):
            with self.assertRaises(asyncio.CancelledError):
                await self.control.change(100, self.pid, 42, 'ipv4', 11, 10)
        self.assertTrue(self.store.ip_changes())
        self.assertEqual(await self.control.recover(self.store.ip_changes()[0]), 'old')
        self.assertEqual(self.current, 10)

    async def test_action_is_polled_without_repeating_the_mutation(self):
        self.cloud.execute.side_effect = None
        self.cloud.execute.return_value = {'action': {'id': 7, 'status': 'running'}}
        self.cloud.execute.side_effect = [
            {'action': {'id': 7, 'status': 'running'}}, {'action': {'id': 7, 'status': 'success'}}]
        with patch('bot.ip_control.asyncio.sleep', new=AsyncMock()):
            await self.control.action('secret-token', 'unassign_primary_ip', 10)
        self.assertEqual([call.args[1]['id'] for call in self.cloud.execute.call_args_list],
                         ['unassign_primary_ip', 'get_action'])


class LanguageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = LANGUAGE.set('fa')
        self.directory = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.store = Store(self.directory.name, self.key)
        self.pid = self.store.add(100, 'نام من {kept}', 'token')
        self.manager = Manager(self.store, SimpleNamespace(execute=AsyncMock()), {100, 200})
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        LANGUAGE.reset(self.token)
        self.directory.cleanup()

    async def test_language_is_persisted_and_other_users_keep_their_language(self):
        await self.manager.callback(update(callback='lang:en'), self.context)
        self.assertEqual(Store(self.directory.name, self.key).languages(), {100: 'en'})
        event = update(callback=f'p:{self.pid}')
        await self.manager.callback(event, self.context)
        text = event.effective_message.reply_text.call_args.args[0]
        self.assertIn('What would you like', text)
        self.assertIn('نام من {kept}', text)
        other = update(owner=200)
        await self.manager.projects(other, SimpleNamespace(user_data={}))
        self.assertIn('پروژه', other.effective_message.reply_text.call_args.args[0])

    async def test_switching_language_cancels_old_pending_input(self):
        self.context.user_data['pending'] = object()
        await self.manager.callback(update(callback='language'), self.context)
        self.assertEqual(self.context.user_data, {})

    async def test_group_or_unauthorized_user_cannot_save_preferences(self):
        for event in (update(owner=999, callback='lang:en'), update(private=False, callback='lang:en')):
            await self.manager.callback(event, self.context)
        self.assertEqual(self.store.languages(), {})


class TranslationTests(unittest.TestCase):
    def test_all_messages_translated_and_placeholders_preserved(self):
        fields = lambda text: sorted(x for _, x, _, _ in Formatter().parse(text) if x is not None)
        for source, english in ENGLISH.items():
            with self.subTest(source=source):
                self.assertTrue(english)
                self.assertEqual(fields(source), fields(english))

    def test_every_static_translation_call_has_a_catalog_entry(self):
        for path in Path('bot').glob('*.py'):
            if path.name == 'i18n.py':
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr':
                    if node.args and isinstance(node.args[0], ast.Constant):
                        self.assertIn(node.args[0].value, ENGLISH, f'{path}:{node.lineno}')

    def test_translation_does_not_interpret_or_translate_user_supplied_values(self):
        token = LANGUAGE.set('en')
        try:
            value = tr('📁 {0}\nچه کاری می‌خواهی انجام بدهی؟', 'سرور {secret}')
            self.assertIn('سرور {secret}', value)
        finally:
            LANGUAGE.reset(token)

    def test_upgrade_reads_the_previous_project_database_format(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Fernet.generate_key()
            db = sqlite3.connect(Path(directory) / 'projects.db')
            try:
                db.execute('CREATE TABLE projects (id TEXT PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL, token BLOB NOT NULL, UNIQUE(owner, name))')
                db.execute('INSERT INTO projects VALUES (?, ?, ?, ?)', ('old-id', 100, 'old-project', Fernet(key).encrypt(b'old-token')))
                db.commit()
            finally:
                db.close()
            store = Store(directory, key.decode())
            self.assertEqual(store.get(100, 'old-id'), ('old-project', 'old-token'))
            self.assertEqual(store.languages(), {})
            self.assertEqual(store.ip_changes(), [])


class ServerSectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.language = LANGUAGE.set('fa')
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, Fernet.generate_key().decode())
        self.pid = self.store.add(100, 'project', 'secret-token')
        self.server = {'id': 42, 'name': 'web', 'status': 'off', 'locked': False,
                       'public_net': {'ipv4': None, 'ipv6': None, 'firewalls': [{'id': 31}]},
                       'server_type': {'architecture': 'x86'}, 'volumes': [21], 'private_net': [{'network': 91}],
                       'datacenter': {'location': {'name': 'fsn1'}}}
        self.cloud = SimpleNamespace(execute=AsyncMock(side_effect=self.execute))
        self.manager = Manager(self.store, self.cloud, {100})
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        LANGUAGE.reset(self.language)
        self.directory.cleanup()

    async def execute(self, token, operation, payload):
        prepare(operation, payload)
        op = operation['id']
        if op == 'get_server':
            return {'server': copy.deepcopy(self.server)}
        if op == 'list_volumes':
            return {'volumes': [{'id': 22, 'name': 'free', 'server': None, 'location': {'name': 'fsn1'}},
                               {'id': 23, 'name': 'remote', 'server': None, 'location': {'name': 'hel1'}}]}
        if op == 'list_firewalls':
            return {'firewalls': [{'id': 32, 'name': 'new-firewall', 'applied_to': []}]}
        if op == 'list_load_balancers':
            return {'load_balancers': [{'id': 33, 'name': 'balancer', 'targets': []}]}
        if op == 'list_networks':
            return {'networks': [{'id': 91, 'name': 'attached'}, {'id': 92, 'name': 'free'}]}
        if op == 'list_isos':
            return {'isos': [{'id': 100, 'name': 'x86-iso', 'architecture': 'x86'},
                             {'id': 101, 'name': 'arm-iso', 'architecture': 'arm'}]}
        if op == 'get_server_metrics':
            return {'metrics': {'time_series': {'cpu': {'values': [[1, '3'], [61, '5']]}}}}
        return {'action': {'id': 7, 'status': 'success'}}

    async def test_all_sections_are_inside_the_selected_server_and_fit_telegram_callbacks(self):
        self.server['id'] = 9007199254740991
        event = update(callback=f"srv:{self.pid}:{self.server['id']}")
        await self.manager.callback(event, self.context)
        markup = event.effective_message.reply_text.call_args.kwargs['reply_markup']
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        for section in ('graphs', 'network', 'firewall', 'volume', 'lb', 'iso', 'rescue', 'scale', 'rebuild', 'delete'):
            self.assertIn(f"sub:{self.pid}:{self.server['id']}:{section}", callbacks)
        self.assertTrue(all(len(value.encode()) <= 64 for value in callbacks))

    async def test_bindings_use_the_selected_server_and_require_confirmation(self):
        expected = {'volume': ('attach_volume', {'assignee': {'type': 'server', 'id': 42}}),
                    'firewall': ('apply_firewall_to_resources', {'apply_to': [{'type': 'server', 'server': {'id': 42}}]}),
                    'lb': ('add_load_balancer_target', {'type': 'server', 'server': {'id': 42}})}
        for category, (operation, body) in expected.items():
            await self.manager.callback(update(callback=f'bind:{self.pid}:42:{category}'), self.context)
            state = self.context.user_data['input']
            if category == 'volume':
                self.assertEqual([x['id'] for x in state['options']], [22])
            await self.manager.callback(update(callback=f"choice:{state['nonce']}:pick:0"), self.context)
            pending = self.context.user_data['pending']
            self.assertEqual(pending.operation, operation)
            self.assertEqual(pending.payload['body'], body)
            self.assertEqual(self.context.user_data['nav'], {'pid': self.pid, 'server': 42})
        self.assertFalse(any(call.args[1]['method'] != 'GET' for call in self.cloud.execute.call_args_list))

    async def test_iso_and_network_choices_are_compatible_and_server_scoped(self):
        for operation, expected in [('attach_server_iso', ['100']), ('attach_server_to_network', [92]),
                                    ('detach_server_from_network', [91])]:
            await self.manager.simple.start(update(), self.context, self.pid, operation, 42)
            state = self.context.user_data['input']
            self.assertEqual([option[0] for option in state['options']], expected)
            await self.manager.simple.callback(update(), self.context, ['easy', state['nonce'], 'pick', '0'])
            self.assertEqual(self.context.user_data['pending'].payload['path'], {'id': 42})

    async def test_firewall_removal_prefills_the_selected_server(self):
        self.context.user_data['nav'] = {'pid': self.pid, 'server': 42}
        from bot.api import OPERATIONS
        await self.manager.forms.start(update(), self.context, self.pid,
                                        OPERATIONS.index(BY_ID['remove_firewall_from_resources']), {'id': 31})
        payload = self.context.user_data['input']['payload']
        self.assertEqual(payload['body']['remove_from'], [{'type': 'server', 'server': {'id': 42}}])
        self.assertNotIn('path', self.context.user_data['input']['keys'])

    async def test_new_ip_from_the_server_uses_its_location(self):
        self.context.user_data['nav'] = {'pid': self.pid, 'server': 42}
        await self.manager.simple.start(update(), self.context, self.pid, 'create_primary_ip')
        await self.manager.simple.text(update(), self.context, 'new-ip')
        state = self.context.user_data['input']
        await self.manager.simple.callback(update(), self.context, ['easy', state['nonce'], 'pick', '0'])
        self.assertEqual(self.context.user_data['pending'].payload['body']['location'], 'fsn1')

    async def test_metric_button_sends_a_real_png_without_asking_for_server_id(self):
        event = update(callback=f'metric:{self.pid}:42:cpu')
        event.effective_message.reply_photo = AsyncMock()
        await self.manager.callback(event, self.context)
        event.effective_message.reply_photo.assert_awaited_once()
        image = event.effective_message.reply_photo.call_args.args[0].getvalue()
        self.assertTrue(image.startswith(b'\x89PNG'))


class GraphTests(unittest.TestCase):
    def test_chart_produces_png_and_ignores_nonfinite_points(self):
        png = render_chart('cpu', {'time_series': {'cpu': {'values': [[1, '1'], [61, '2'], [120, 'NaN']]}}})
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertGreater(len(png), 10000)

    def test_empty_chart_is_reported(self):
        with self.assertRaises(ValueError):
            render_chart('cpu', {'time_series': {}})


if __name__ == '__main__':
    unittest.main()
