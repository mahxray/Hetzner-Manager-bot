import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from bot.api import BY_ID, OPERATIONS, prepare, schema_for
from bot.forms import SOURCES, node_schema, resolve, scalar
from bot.main import COLLECTIONS, Manager
from bot.storage import Store
from test_bot import update


class FormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, Fernet.generate_key().decode())
        self.pid = self.store.add(100, 'production', 'private-project-token')
        self.cloud = SimpleNamespace(execute=AsyncMock(return_value={'action': {'id': 7}}))
        self.manager = Manager(self.store, self.cloud, {100, 200})
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def start(self, operation, path=None):
        await self.manager.forms.start(update(), self.context, self.pid,
                                       OPERATIONS.index(BY_ID[operation]), path)

    async def click(self, action):
        nonce = self.context.user_data['input']['nonce']
        await self.manager.callback(update(callback=f'flow:{nonce}:{action}'), self.context)

    async def field(self, name):
        state = self.context.user_data['input']
        i = state['keys'].index(name)
        while i // 8 > state['page']:
            await self.click('page:1')
        while i // 8 < state['page']:
            await self.click('page:-1')
        await self.click(f'field:{i}')

    async def text(self, value):
        await self.manager.text(update(text=value), self.context)

    async def complete_to_preview(self):
        while self.context.user_data.get('input', {}).get('cursor'):
            await self.click('done')
        await self.click('done')

    async def test_network_and_nested_subnet_without_json(self):
        await self.start('create_network')
        await self.field('body')
        await self.field('name')
        await self.text('private-network')
        await self.field('ip_range')
        await self.text('10.0.0.0/16')
        await self.field('subnets')
        await self.click('append')
        await self.field('type')
        await self.click('value:0')
        await self.field('network_zone')
        await self.text('eu-central')
        await self.field('ip_range')
        await self.text('10.0.1.0/24')
        await self.complete_to_preview()
        pending = self.context.user_data['pending']
        self.assertEqual(pending.payload['body']['subnets'], [
            {'type': 'cloud', 'network_zone': 'eu-central', 'ip_range': '10.0.1.0/24'}])
        self.cloud.execute.assert_not_awaited()

    async def test_firewall_rule_and_cidr_array_without_json(self):
        await self.start('create_firewall')
        await self.field('body')
        await self.field('name')
        await self.text('web')
        await self.field('rules')
        await self.click('append')
        await self.field('direction')
        await self.click('value:0')
        await self.field('protocol')
        await self.click('value:0')
        await self.field('port')
        await self.text('443')
        await self.field('source_ips')
        await self.click('append')
        await self.text('0.0.0.0/0')
        await self.complete_to_preview()
        rule = self.context.user_data['pending'].payload['body']['rules'][0]
        self.assertEqual(rule, {'direction': 'in', 'protocol': 'tcp', 'port': '443', 'source_ips': ['0.0.0.0/0']})
        self.cloud.execute.assert_not_awaited()

    async def test_dynamic_labels_and_multiline_input(self):
        await self.start('create_ssh_key')
        await self.field('body')
        await self.field('name')
        await self.text('deploy')
        await self.field('public_key')
        await self.text('ssh-ed25519 AAAAexample test')
        await self.field('labels')
        await self.click('map')
        await self.text('environment')
        await self.text('production')
        await self.complete_to_preview()
        self.assertEqual(self.context.user_data['pending'].payload['body']['labels'], {'environment': 'production'})

    async def test_multiline_file_field_preserves_whitespace(self):
        await self.start('create_server')
        await self.field('body')
        await self.field('user_data')
        event = update()
        content = '#cloud-config\nusers:\n  - name: deploy\n'
        remote_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(content.encode())))
        event.effective_message.document = SimpleNamespace(file_size=len(content),
                                                            get_file=AsyncMock(return_value=remote_file))
        await self.manager.document(event, self.context)
        self.assertEqual(self.context.user_data['input']['payload']['body']['user_data'], content)
        self.cloud.execute.assert_not_awaited()

    async def test_array_remove_and_optional_field_omission(self):
        await self.start('create_network')
        await self.field('body')
        await self.field('name')
        await self.text('test')
        await self.field('name')
        await self.click('remove')
        self.assertNotIn('name', self.context.user_data['input']['payload']['body'])
        await self.field('subnets')
        await self.click('append')
        await self.click('remove')
        self.assertEqual(self.context.user_data['input']['payload']['body']['subnets'], [])

    async def test_dns_record_and_encoded_wildcard_path(self):
        await self.start('create_zone_rrset', {'id_or_name': 'example.com'})
        await self.field('body')
        await self.field('name')
        await self.text('www')
        await self.field('type')
        schema = node_schema(self.context.user_data['input'])
        await self.click('value:' + str(schema['enum'].index('A')))
        await self.field('records')
        await self.click('append')
        await self.field('value')
        await self.text('198.51.100.1')
        await self.complete_to_preview()
        pending = self.context.user_data['pending']
        self.assertEqual(pending.payload['path']['id_or_name'], 'example.com')
        self.assertEqual(pending.payload['body']['records'][0]['value'], '198.51.100.1')
        path, _, _ = prepare(BY_ID['delete_zone_rrset'], {'path': {
            'id_or_name': 'example.com', 'rr_name': '*.test', 'rr_type': 'A'}})
        self.assertEqual(path, '/zones/example.com/rrsets/%2A.test/A')

    async def test_real_project_resource_picker(self):
        await self.start('create_server')
        await self.field('body')
        await self.field('server_type')
        self.cloud.execute.return_value = {'server_types': [{'id': 8, 'name': 'cx23', 'cores': 2, 'memory': 4,
                                                            'locations': [{'name': 'fsn1', 'available': True}]}],
                                           'meta': {'pagination': {'next_page': None}}}
        await self.click('pick:1')
        self.cloud.execute.assert_awaited_once_with('private-project-token', BY_ID['list_server_types'],
                                                   {'query': {'page': 1, 'per_page': 50}})
        await self.click('choose:0')
        self.assertEqual(self.context.user_data['input']['payload']['body']['server_type'], 'cx23')

    async def test_bool_and_bound_server_operation(self):
        await self.start('change_server_type', {'id': 42})
        await self.field('body')
        await self.field('upgrade_disk')
        await self.click('value:1')
        await self.field('server_type')
        await self.text('cx23')
        await self.complete_to_preview()
        pending = self.context.user_data['pending']
        self.assertEqual(pending.payload, {'path': {'id': 42}, 'body': {'upgrade_disk': False, 'server_type': 'cx23'}})

    async def test_missing_required_fields_cannot_submit(self):
        await self.start('create_network')
        await self.field('body')
        with self.assertRaises(ValueError):
            await self.click('done')
        self.cloud.execute.assert_not_awaited()
        self.assertNotIn('pending', self.context.user_data)

    async def test_old_buttons_expiry_and_switching_projects(self):
        await self.start('create_network')
        nonce = self.context.user_data['input']['nonce']
        await self.field('body')
        with self.assertRaises(ValueError):
            await self.manager.callback(update(callback=f'flow:{nonce}:done'), self.context)
        self.context.user_data['input']['expires'] = 0
        with self.assertRaises(ValueError):
            await self.click('done')
        await self.start('create_network')
        nonce = self.context.user_data['input']['nonce']
        await self.manager.callback(update(callback=f'p:{self.pid}'), self.context)
        with self.assertRaises(ValueError):
            await self.manager.callback(update(callback=f'flow:{nonce}:done'), self.context)
        self.cloud.execute.assert_not_awaited()

    async def test_other_owner_cannot_use_project_form(self):
        with self.assertRaises(ValueError):
            await self.manager.forms.start(update(owner=200), self.context, self.pid,
                                           OPERATIONS.index(BY_ID['delete_server']))

    async def test_list_resources_and_creation_button(self):
        self.cloud.execute.return_value = {'networks': [{'id': 91, 'name': 'private'}],
                                           'meta': {'pagination': {'next_page': 2}}}
        event = update()
        i = next(i for i, x in enumerate(COLLECTIONS) if x[0] == 'networks')
        await self.manager.resources(event, self.pid, i, 1)
        rendered = str(event.effective_message.reply_text.call_args_list)
        self.assertIn('private', rendered)
        self.assertIn(f'item:{self.pid}:{i}:91:0', rendered)
        self.assertIn('form:', rendered)


class FormSchemaTests(unittest.TestCase):
    def test_all_operations_root_schemas_resolve(self):
        for index, operation in enumerate(OPERATIONS):
            with self.subTest(operation=operation['id']):
                state = {'index': index, 'cursor': [], 'variants': {}}
                schema = node_schema(state)
                self.assertEqual(schema['type'], 'object')
                for group in schema['properties']:
                    state['cursor'] = [group]
                    self.assertIsInstance(node_schema(state), dict)

    def test_all_resource_pickers_accept_pagination(self):
        for collection, _ in SOURCES.values():
            with self.subTest(collection=collection):
                prepare(BY_ID['list_' + collection], {'query': {'page': 1, 'per_page': 8}})

    def test_scalar_validation_rejects_nonfinite_and_wrong_types(self):
        for value in ['nan', 'inf', '-inf']:
            with self.assertRaises(ValueError):
                scalar(value, {'type': 'number'})
        with self.assertRaises(ValueError):
            scalar('abc', {'type': 'integer'})
        with self.assertRaises(ValueError):
            scalar('other', {'type': 'string', 'enum': ['one']})

    def test_union_branch_resolution(self):
        schema = resolve({'allOf': [{'type': 'object', 'properties': {'a': {'type': 'integer'}}, 'required': ['a']},
                                    {'type': 'object', 'properties': {'b': {'type': 'string'}}, 'required': ['b']}]})
        self.assertEqual(set(schema['required']), {'a', 'b'})
        self.assertEqual(set(schema['properties']), {'a', 'b'})


if __name__ == '__main__':
    unittest.main()
