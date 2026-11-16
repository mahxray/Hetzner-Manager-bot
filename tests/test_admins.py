import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.api import BY_ID, OPERATIONS
from bot.i18n import LANGUAGE
from bot.main import Manager, Pending
from bot.storage import Store
from test_bot import update


class GrantStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.store = Store(self.directory.name, self.key)
        self.first = self.store.add(100, 'first', 'token-first')
        self.second = self.store.add(100, 'second', 'token-second')
        self.other = self.store.add(200, 'other', 'token-other')
        self.store.add_admin(100, 300, 'Alice')

    def tearDown(self):
        self.directory.cleanup()

    def test_only_explicitly_granted_projects_are_visible_and_accessible(self):
        self.store.replace_grants(100, 300, {self.first})
        self.assertEqual(self.store.granted_projects(300), [(self.first, 'first')])
        self.assertEqual(self.store.get(300, self.first), ('first', 'token-first'))
        with self.assertRaises(ValueError):
            self.store.get(300, self.second)
        with self.assertRaises(ValueError):
            self.store.get(300, self.other)
        new_project = self.store.add(100, 'new', 'new-token')
        self.assertFalse(self.store.has_grant(300, new_project))

    def test_grants_and_admins_survive_reopening_the_database(self):
        self.store.replace_grants(100, 300, {self.first, self.second})
        reopened = Store(self.directory.name, self.key)
        self.assertEqual(reopened.admin_ids(), {300})
        self.assertEqual(reopened.assigned_projects(100, 300), {self.first, self.second})
        self.assertEqual(reopened.get(300, self.first)[1], 'token-first')

    def test_owner_cannot_delegate_another_owners_project_and_old_grants_remain(self):
        self.store.replace_grants(100, 300, {self.first})
        with self.assertRaises(ValueError):
            self.store.replace_grants(100, 300, {self.second, self.other})
        self.assertEqual(self.store.assigned_projects(100, 300), {self.first})

    def test_failed_multi_project_write_rolls_back_the_entire_change(self):
        self.store.replace_grants(100, 300, {self.first})
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER test_reject_grant BEFORE INSERT ON project_grants
                WHEN (SELECT name FROM projects WHERE id=NEW.project)='second'
                BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.replace_grants(100, 300, {self.first, self.second})
        self.assertEqual(self.store.assigned_projects(100, 300), {self.first})

    def test_removal_only_revokes_the_selected_owners_grants(self):
        self.store.add_admin(200, 300, 'Alice too')
        self.store.replace_grants(100, 300, {self.first})
        self.store.replace_grants(200, 300, {self.other})
        self.store.remove_admin(100, 300)
        self.assertEqual(self.store.admin_ids(), {300})
        self.assertEqual(self.store.granted_projects(300), [(self.other, 'other')])
        with self.assertRaises(ValueError):
            self.store.get(300, self.first)

    def test_forgotten_project_removes_its_grants_without_deleting_admin(self):
        self.store.replace_grants(100, 300, {self.first})
        self.store.remove(100, self.first)
        self.assertEqual(self.store.granted_projects(300), [])
        self.assertEqual(self.store.admin_ids(), {300})

    def test_project_owner_cannot_forget_a_delegates_unfinished_ip_change(self):
        self.store.begin_ip_change(300, self.first, 42, 'ipv4', 10, 11)
        with self.assertRaises(ValueError):
            self.store.remove(100, self.first)
        self.assertEqual(self.store.project_owner(self.first), 100)


class AdminFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.language = LANGUAGE.set('fa')
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, Fernet.generate_key().decode())
        self.first = self.store.add(100, 'first', 'token-first')
        self.second = self.store.add(100, 'second', 'token-second')
        self.other = self.store.add(200, 'other', 'token-other')
        self.cloud = SimpleNamespace(execute=AsyncMock(return_value={'action': {'id': 7, 'status': 'success'}}))
        self.manager = Manager(self.store, self.cloud, {100, 200})
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        LANGUAGE.reset(self.language)
        self.directory.cleanup()

    async def grant(self, projects):
        if 300 not in dict(self.store.list_admins(100)):
            self.store.add_admin(100, 300, 'Alice')
        self.store.replace_grants(100, 300, projects)
        await self.manager.admins.refresh_roles()

    async def click_grants(self, action):
        nonce = self.context.user_data['input']['nonce']
        await self.manager.callback(update(callback=f'adminpick:{nonce}:{action}'), self.context)

    async def test_owner_can_add_admin_then_save_only_selected_projects(self):
        await self.manager.callback(update(callback='adminadd'), self.context)
        await self.manager.text(update(text='300'), self.context)
        await self.manager.text(update(text='Alice'), self.context)
        self.assertEqual(self.context.user_data['input']['options'], [(self.first, 'first'), (self.second, 'second')])
        await self.click_grants('toggle:0')
        self.assertEqual(self.store.granted_projects(300), [])
        await self.click_grants('save')
        self.assertEqual(self.store.granted_projects(300), [(self.first, 'first')])
        event = update(owner=300)
        await self.manager.projects(event, SimpleNamespace(user_data={}))
        markup = event.effective_message.reply_text.call_args.kwargs['reply_markup']
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn(f'p:{self.first}', callbacks)
        self.assertNotIn(f'p:{self.second}', callbacks)
        self.assertNotIn('add', callbacks)
        self.assertNotIn('admins:0', callbacks)
        self.cloud.execute.assert_not_awaited()

    async def test_admin_can_execute_a_confirmed_action_on_a_granted_project(self):
        await self.grant({self.first})
        context = SimpleNamespace(user_data={})
        await self.manager.callback(update(owner=300, callback=f'quick:{self.first}:42:del'), context)
        self.cloud.execute.assert_not_awaited()
        nonce = context.user_data['pending'].nonce
        await self.manager.callback(update(owner=300, callback=f'yes:{nonce}'), context)
        self.cloud.execute.assert_awaited_once_with('token-first', BY_ID['delete_server'], {'path': {'id': 42}})

    async def test_admin_cannot_add_projects_manage_admins_or_forget_assigned_project(self):
        await self.grant({self.first})
        for callback in ('add', 'adminadd', 'admins:0', 'admin:300', f'forget:{self.first}'):
            with self.subTest(callback=callback), self.assertRaises(ValueError):
                await self.manager.callback(update(owner=300, callback=callback), SimpleNamespace(user_data={}))
        context = SimpleNamespace(user_data={'input': {'kind': 'token', 'name': 'injected', 'expires': time.monotonic()+60}})
        with self.assertRaises(ValueError):
            await self.manager.text(update(owner=300, text='a'*64), context)
        self.cloud.execute.assert_not_awaited()

    async def test_admin_project_settings_hide_connection_removal(self):
        await self.grant({self.first})
        event = update(owner=300, callback=f'settings:{self.first}')
        await self.manager.callback(event, SimpleNamespace(user_data={}))
        callbacks = [b.callback_data for row in event.effective_message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertNotIn(f'forget:{self.first}', callbacks)

    async def test_forged_project_button_is_denied_before_any_cloud_request(self):
        await self.grant({self.first})
        with self.assertRaises(ValueError):
            await self.manager.callback(update(owner=300, callback=f'servers:{self.other}:1'), SimpleNamespace(user_data={}))
        self.cloud.execute.assert_not_awaited()

    async def test_revocation_blocks_an_existing_confirmation_even_when_admin_has_other_projects(self):
        await self.grant({self.first, self.second})
        context = SimpleNamespace(user_data={})
        await self.manager.callback(update(owner=300, callback=f'quick:{self.first}:42:del'), context)
        nonce = context.user_data['pending'].nonce
        self.store.replace_grants(100, 300, {self.second})
        with self.assertRaises(ValueError):
            await self.manager.callback(update(owner=300, callback=f'yes:{nonce}'), context)
        self.cloud.execute.assert_not_awaited()

    async def test_revocation_blocks_open_forms_and_sensitive_receipts(self):
        await self.grant({self.first, self.second})
        context = SimpleNamespace(user_data={})
        await self.manager.forms.start(update(owner=300), context, self.first, OPERATIONS.index(BY_ID['create_server']))
        nonce = context.user_data['input']['nonce']
        self.store.replace_grants(100, 300, {self.second})
        with self.assertRaises(ValueError):
            await self.manager.callback(update(owner=300, callback=f'flow:{nonce}:done'), context)
        context.user_data.clear()
        context.user_data['receipt'] = {'nonce': 'test', 'expires': time.monotonic()+60,
                                        'project': self.first, 'value': {'root_password': 'never disclose'}}
        event = update(owner=300, callback='reveal:test')
        with self.assertRaises(ValueError):
            await self.manager.callback(event, context)
        event.effective_message.reply_text.assert_not_awaited()
        self.cloud.execute.assert_not_awaited()

    async def test_removing_admin_requires_confirmation_and_revokes_without_restart(self):
        await self.grant({self.first})
        await self.manager.callback(update(callback='adminremove:300'), self.context)
        self.assertTrue(self.manager.authorized(update(owner=300)))
        nonce = self.context.user_data['input']['nonce']
        await self.manager.callback(update(callback=f'adminconfirm:{nonce}'), self.context)
        self.assertFalse(self.manager.authorized(update(owner=300)))
        self.assertFalse(self.store.has_grant(300, self.first))

    async def test_owner_cannot_edit_another_owners_admin(self):
        self.store.add_admin(200, 400, 'Other admin')
        for callback in ('admin:400', 'admingrants:400', 'adminremove:400'):
            with self.assertRaises(ValueError):
                await self.manager.callback(update(callback=callback), self.context)

    async def test_cancelled_or_stale_grant_picker_does_not_write(self):
        await self.grant({self.first})
        await self.manager.admins.edit_grants(update(), self.context, 300)
        old_nonce = self.context.user_data['input']['nonce']
        await self.click_grants('toggle:1')
        with self.assertRaises(ValueError):
            await self.manager.callback(update(callback=f'adminpick:{old_nonce}:save'), self.context)
        await self.manager.callback(update(callback='admin:300'), self.context)
        self.assertEqual(self.store.assigned_projects(100, 300), {self.first})

    async def test_a_former_owner_now_added_as_admin_does_not_regain_old_owned_projects(self):
        self.store.add_admin(200, 100, 'Former owner')
        self.store.replace_grants(200, 100, {self.other})
        manager = Manager(self.store, self.cloud, {200})
        event = update(owner=100)
        await manager.projects(event, SimpleNamespace(user_data={}))
        callbacks = [b.callback_data for row in event.effective_message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn(f'p:{self.other}', callbacks)
        self.assertNotIn(f'p:{self.first}', callbacks)
        with self.assertRaises(ValueError):
            await manager.project(update(owner=100), self.first)

    async def test_delegation_from_an_owner_removed_from_configuration_is_not_active(self):
        await self.grant({self.first})
        manager = Manager(self.store, self.cloud, {200})
        self.assertFalse(manager.authorized(update(owner=300)))
        self.store.add_admin(200, 300, 'Active admin')
        self.store.replace_grants(200, 300, {self.other})
        manager = Manager(self.store, self.cloud, {200})
        self.assertTrue(manager.authorized(update(owner=300)))
        with self.assertRaises(ValueError):
            await manager.project(update(owner=300), self.first)
        self.assertEqual((await manager.project(update(owner=300), self.other))[0], 'other')

    async def test_project_owner_can_recover_a_revoked_admins_ip_journal(self):
        await self.grant({self.first})
        self.store.begin_ip_change(300, self.first, 42, 'ipv4', 10, 11)
        self.store.remove_admin(100, 300)
        self.cloud.execute.return_value = {'server': {'id': 42, 'status': 'off', 'locked': False,
                                                       'public_net': {'ipv4': {'id': 10}}}}
        event = update(callback=f'iprecover:{self.first}:42')
        self.cloud.execute.return_value['server']['name'] = 'web'
        await self.manager.callback(event, self.context)
        self.assertEqual(self.store.ip_changes(), [])
        self.assertEqual(self.cloud.execute.call_args_list[0].args[0], 'token-first')

    async def test_admin_management_is_translated_in_english(self):
        self.manager.languages[100] = 'en'
        event = update(callback='admins:0')
        await self.manager.callback(event, self.context)
        self.assertIn('Manage admins', event.effective_message.reply_text.call_args.args[0])
