import copy
from datetime import datetime, timezone
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.api import BY_ID, OPERATIONS
from bot.availability import all_items, available_plan
from bot.main import Manager
from bot.simple import server_card
from bot.storage import Store
from test_bot import update

PLAN = {'id': 8, 'name': 'cx23', 'cores': 2, 'memory': 4, 'architecture': 'x86',
        'locations': [{'name': 'fsn1', 'available': True, 'deprecation': None}],
        'prices': [{'location': 'fsn1', 'price_monthly': {'gross': '4.99'}}]}
SERVER = {'id': 42, 'name': 'web', 'status': 'off', 'server_type': PLAN,
          'datacenter': {'location': {'name': 'fsn1', 'city': 'Falkenstein'}},
          'public_net': {'ipv4': {'ip': '198.51.100.1'}, 'ipv6': None}}


class AvailabilityTests(unittest.TestCase):
    def test_only_live_available_capacity_at_selected_location(self):
        self.assertTrue(available_plan(PLAN, 'fsn1'))
        self.assertFalse(available_plan(PLAN, 'nbg1'))
        for value in (False, None, 'true'):
            plan = copy.deepcopy(PLAN)
            plan['locations'][0]['available'] = value
            self.assertFalse(available_plan(plan, 'fsn1'))
        self.assertFalse(available_plan({'id': 8}))

    def test_per_location_retirement_not_the_legacy_global_field(self):
        plan = copy.deepcopy(PLAN)
        plan['deprecation'] = {'unavailable_after': '2020-01-01T00:00:00Z'}
        now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        self.assertTrue(available_plan(plan, 'fsn1', now))
        plan['locations'][0]['deprecation'] = {'unavailable_after': '2026-01-01T00:00:00Z'}
        self.assertFalse(available_plan(plan, 'fsn1', now))

    def test_server_card_is_human_readable(self):
        text = server_card(SERVER, 'production')
        self.assertIn('خاموش', text)
        self.assertIn('198.51.100.1', text)
        self.assertNotIn('public_net', text)


class SimpleFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, Fernet.generate_key().decode())
        self.pid = self.store.add(100, 'production', 'project-token')
        self.available = True
        self.requests = []
        self.cloud = SimpleNamespace(execute=AsyncMock(side_effect=self.respond))
        self.manager = Manager(self.store, self.cloud, {100, 200})
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def respond(self, token, operation, payload):
        self.requests.append((operation['id'], payload))
        name = operation['id']
        if name == 'list_server_types':
            live = copy.deepcopy(PLAN)
            live['locations'][0]['available'] = self.available
            soldout = copy.deepcopy(PLAN)
            soldout.update(id=9, name='soldout')
            soldout['locations'][0]['available'] = False
            return {'server_types': [soldout, live]}
        if name == 'get_pricing':
            return {'pricing': {'currency': 'USD'}}
        if name == 'list_locations':
            return {'locations': [{'id': 1, 'name': 'fsn1', 'country': 'DE', 'city': 'Falkenstein'},
                                  {'id': 2, 'name': 'empty', 'country': 'US', 'city': 'No stock'}]}
        if name == 'list_images':
            return {'images': [{'id': 10, 'name': 'ubuntu-24.04', 'description': 'Ubuntu 24.04', 'architecture': 'x86'}]}
        if name == 'get_image':
            return {'image': {'id': 10, 'description': 'my-snapshot', 'architecture': 'x86'}}
        if name == 'list_ssh_keys':
            return {'ssh_keys': []}
        if name == 'get_server':
            return {'server': copy.deepcopy(SERVER)}
        if name == 'list_servers':
            return {'servers': [copy.deepcopy(SERVER)]}
        if name == 'get_primary_ip':
            return {'primary_ip': {'id': 71, 'name': 'my-ip', 'ip': '198.51.100.1', 'location': {'name': 'fsn1'}}}
        return {'action': {'id': 7, 'status': 'running'}}

    async def start(self, operation, resource=None, image=None):
        await self.manager.simple.start(update(), self.context, self.pid, operation, resource, image)

    async def click(self, action):
        nonce = self.context.user_data['input']['nonce']
        await self.manager.callback(update(callback=f'easy:{nonce}:{action}'), self.context)

    async def create_to_confirmation(self):
        await self.start('create_server')
        await self.manager.text(update(text='my-server'), self.context)
        options = self.context.user_data['input']['options']
        self.assertEqual([x[0] for x in options], ['fsn1'])
        await self.click('pick:0')
        options = self.context.user_data['input']['options']
        self.assertEqual([x[0] for x in options], ['cx23'])
        self.assertIn('USD', options[0][1])
        self.assertNotIn('€', options[0][1])
        await self.click('pick:0')
        await self.click('pick:0')
        await self.click('skip')

    async def test_sequential_creation_filters_stock_and_has_no_mutation_before_confirmation(self):
        await self.create_to_confirmation()
        pending = self.context.user_data['pending']
        self.assertTrue(pending.simple)
        self.assertEqual(pending.payload, {'body': {'name': 'my-server', 'location': 'fsn1',
                                                   'server_type': 'cx23', 'image': '10'}})
        self.assertFalse(any(name == 'create_server' for name, _ in self.requests))
        await self.manager.callback(update(callback=f'yes:{pending.nonce}'), self.context)
        self.assertEqual(sum(name == 'create_server' for name, _ in self.requests), 1)

    async def test_stock_is_rechecked_on_final_confirmation(self):
        await self.create_to_confirmation()
        pending = self.context.user_data['pending']
        self.available = False
        with self.assertRaises(ValueError):
            await self.manager.callback(update(callback=f'yes:{pending.nonce}'), self.context)
        self.assertFalse(any(name == 'create_server' for name, _ in self.requests))

    async def test_expired_and_old_wizard_buttons_are_rejected(self):
        await self.start('create_server')
        nonce = self.context.user_data['input']['nonce']
        await self.manager.text(update(text='my-server'), self.context)
        with self.assertRaises(ValueError):
            await self.manager.callback(update(callback=f'easy:{nonce}:pick:0'), self.context)
        self.context.user_data['input']['expires'] = 0
        with self.assertRaises(ValueError):
            await self.click('pick:0')

    async def test_going_back_to_image_keeps_the_selected_architecture(self):
        await self.start('create_server')
        await self.manager.text(update(text='my-server'), self.context)
        await self.click('pick:0')
        await self.click('pick:0')
        await self.click('pick:0')
        await self.click('back')
        self.assertEqual(self.context.user_data['input']['architecture'], 'x86')
        image_calls = [payload for name, payload in self.requests if name == 'list_images']
        self.assertEqual(image_calls[-1]['query']['architecture'], 'x86')

    async def test_simple_server_action_is_bound_and_has_a_friendly_confirmation(self):
        event = update()
        await self.manager.simple.start(event, self.context, self.pid, 'delete_server', 42)
        pending = self.context.user_data['pending']
        self.assertEqual(pending.payload, {'path': {'id': 42}})
        text = str(event.effective_message.reply_text.call_args_list)
        self.assertIn('web', text)
        self.assertNotIn('/servers/{id}', text)
        self.assertNotIn('DELETE', text)

    async def test_existing_servers_are_loaded_live_without_invented_entries(self):
        event = update()
        await self.manager.servers(event, self.pid, 1)
        rendered = str(event.effective_message.reply_text.call_args_list)
        self.assertIn(f'srv:{self.pid}:42', rendered)
        self.assertNotIn('srv:' + self.pid + ':999', rendered)
        self.assertEqual(self.requests[-1][0], 'list_servers')

    async def test_deleting_servers_are_hidden_but_off_servers_remain_visible(self):
        deleting = {**SERVER, 'id': 999, 'status': 'deleting'}
        self.cloud.execute.side_effect = None
        self.cloud.execute.return_value = {'servers': [SERVER, deleting]}
        event = update()
        await self.manager.servers(event, self.pid, 1)
        rendered = str(event.effective_message.reply_text.call_args_list)
        self.assertIn(f'srv:{self.pid}:42', rendered)
        self.assertNotIn(f'srv:{self.pid}:999', rendered)

    async def test_simple_password_result_has_no_raw_server_payload(self):
        self.cloud.execute.side_effect = None
        self.cloud.execute.return_value = {'root_password': 'private-password', 'action': {'id': 7, 'status': 'success'}}
        event = update()
        await self.manager.execute(event, self.context, self.pid, BY_ID['reset_server_password'],
                                    {'path': {'id': 42}}, simple=True)
        self.assertNotIn('private-password', str(event.effective_message.reply_text.call_args_list))
        self.assertEqual(self.context.user_data['receipt']['value'], {'root_password': 'private-password'})
        nonce = self.context.user_data['receipt']['nonce']
        revealed = update(callback=f'reveal:{nonce}')
        await self.manager.callback(revealed, self.context)
        self.assertIn('private-password', str(revealed.effective_message.reply_text.call_args_list))

    async def test_separate_snapshot_restore_chooses_a_real_target(self):
        await self.start('rebuild_server', image=10)
        await self.click('pick:0')
        pending = self.context.user_data['pending']
        self.assertEqual(pending.payload, {'path': {'id': 42}, 'body': {'image': '10'}})

    async def test_primary_ip_creation_uses_current_location_contract(self):
        await self.start('create_primary_ip')
        await self.manager.text(update(text='my-ip'), self.context)
        await self.click('pick:0')
        await self.click('pick:0')
        pending = self.context.user_data['pending']
        self.assertEqual(pending.payload['body'], {'name': 'my-ip', 'type': 'ipv4',
                                                  'assignee_type': 'server', 'location': 'fsn1'})

    async def test_project_home_hides_technical_tools(self):
        event = update()
        await self.manager.menu(event, self.pid)
        rendered = str(event.effective_message.reply_text.call_args_list)
        self.assertNotIn('JSON', rendered)
        self.assertNotIn('tags:', rendered)
        self.assertIn('سرورهای من', rendered)
        markup = event.effective_message.reply_text.call_args.kwargs['reply_markup']
        self.assertIn('اسنپ‌شات', ' '.join(b.text for row in markup.inline_keyboard for b in row))

    async def test_other_owner_cannot_start_wizard(self):
        with self.assertRaises(ValueError):
            await self.manager.simple.start(update(owner=200), self.context, self.pid, 'create_server')

    async def test_pagination_collects_capacity_from_later_pages(self):
        cloud = SimpleNamespace(execute=AsyncMock(side_effect=[
            {'server_types': [{'id': 1}], 'meta': {'pagination': {'next_page': 2}}},
            {'server_types': [{'id': 2}]}]))
        result = await all_items(cloud, 'token', BY_ID['list_server_types'], 'server_types')
        self.assertEqual(result, [{'id': 1}, {'id': 2}])


if __name__ == '__main__':
    unittest.main()
