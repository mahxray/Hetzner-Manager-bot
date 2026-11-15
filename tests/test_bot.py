import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet, InvalidToken
import httpx
from jsonschema import Draft202012Validator

from bot.api import BY_ID, OPERATIONS, Cloud, CloudError, prepare, redact, schema_for
from bot.main import Manager, Pending, consume_pending, expire_state
from bot.storage import Store


def update(owner=100, private=True, callback="", text=""):
    message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock(), delete=AsyncMock(), text=text)
    return SimpleNamespace(effective_user=SimpleNamespace(id=owner),
                           effective_chat=SimpleNamespace(type="private" if private else "group"),
                           effective_message=message,
                           callback_query=SimpleNamespace(data=callback, answer=AsyncMock()))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.store = Store(self.directory.name, self.key)

    def tearDown(self):
        self.directory.cleanup()

    def test_tokens_encrypted_and_owner_isolated(self):
        pid = self.store.add(100, "first", "private-token-1")
        self.assertEqual(self.store.get(100, pid), ("first", "private-token-1"))
        self.assertNotIn(b"private-token-1", Path(self.store.path).read_bytes())
        self.assertEqual(self.store.list(200), [])
        with self.assertRaises(ValueError):
            self.store.get(200, pid)
        self.store.remove(200, pid)
        self.assertEqual(len(self.store.list(100)), 1)

    def test_multiple_projects_and_removal(self):
        first = self.store.add(100, "first", "token-one")
        second = self.store.add(100, "second", "token-two")
        self.store.remove(100, first)
        self.assertEqual(self.store.get(100, second)[1], "token-two")
        with self.assertRaises(ValueError):
            self.store.get(100, first)

    def test_wrong_key_cannot_read(self):
        pid = self.store.add(100, "first", "token")
        wrong = Store(self.directory.name, Fernet.generate_key().decode())
        with self.assertRaises(InvalidToken):
            wrong.get(100, pid)


class CatalogTests(unittest.TestCase):
    def test_all_operations_have_valid_input_schema_and_no_remote_refs(self):
        self.assertGreaterEqual(len(OPERATIONS), 190)
        self.assertEqual(len(BY_ID), len(OPERATIONS))
        for operation in OPERATIONS:
            with self.subTest(operation=operation["id"]):
                schema = schema_for(operation)
                Draft202012Validator.check_schema(schema)
                self.assertNotIn('"$ref": "http', json.dumps(schema))

    def test_required_path_and_request_fields(self):
        with self.assertRaises(ValueError):
            prepare(BY_ID["delete_server"], {})
        with self.assertRaises(ValueError):
            prepare(BY_ID["create_server"], {"body": {"name": "test"}})

    def test_create_and_paginated_query(self):
        path, _, body = prepare(BY_ID["create_server"], {"body": {
            "name": "my-server", "server_type": "cpx22", "image": "ubuntu-24.04"}})
        self.assertEqual(path, "/servers")
        self.assertEqual(body["name"], "my-server")
        self.assertIn(("page", "2"), prepare(BY_ID["list_servers"], {"query": {"page": 2}})[1])

    def test_path_traversal_url_and_unknown_keys_rejected(self):
        for identifier in ("../../pricing", "https://example.com", "42?token=oops", -1):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                prepare(BY_ID["delete_server"], {"path": {"id": identifier}})
        with self.assertRaises(ValueError):
            prepare(BY_ID["list_servers"], {"query": {"api_token": "secret"}})

    def test_redaction_and_explicit_reveal(self):
        value = {"root_password": "pw", "wss_url": "wss://console?ticket=secret",
                 "nested": [{"private_key": "key", "name": "api-secret"}]}
        safe = redact(value, ("api-secret",))
        self.assertNotIn("pw", json.dumps(safe))
        self.assertNotIn("wss://", json.dumps(safe))
        self.assertEqual(safe["nested"][0]["name"], "[REDACTED]")
        self.assertEqual(redact(value, ("api-secret",), hide_fields=False)["root_password"], "pw")

    def test_quick_action_contracts(self):
        for operation in ("poweron_server", "shutdown_server", "reboot_server", "poweroff_server", "delete_server"):
            prepare(BY_ID[operation], {"path": {"id": 42}})
        prepare(BY_ID["create_server_image"], {"path": {"id": 42}, "body": {"type": "snapshot"}})


class ConfirmationTests(unittest.TestCase):
    def test_cleanup_removes_expired_private_state_only(self):
        state = {"pending": Pending("n", "p", "x", {"secret": "value"}, 10),
                 "receipt": {"expires": 10, "value": {"password": "value"}},
                 "input": {"expires": 30, "kind": "operation"}}
        expire_state(state, 20)
        self.assertEqual(state, {"input": {"expires": 30, "kind": "operation"}})

    def test_bound_to_original_project_and_single_use(self):
        state = {"pending": Pending("nonce", "project-a", "delete_server", {"path": {"id": 42}}, 20)}
        with self.assertRaises(ValueError):
            consume_pending(state, "wrong", now=10)
        pending = consume_pending(state, "nonce", now=10)
        self.assertEqual(pending.project, "project-a")
        with self.assertRaises(ValueError):
            consume_pending(state, "nonce", now=10)

    def test_expired_consumed_without_execution(self):
        state = {"pending": Pending("nonce", "p", "delete_server", {}, 10)}
        with self.assertRaises(ValueError):
            consume_pending(state, "nonce", now=11)
        self.assertNotIn("pending", state)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_headers_origin_payload_and_no_redirect(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(201, json={"server": {"id": 42}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await Cloud(client).execute("test-token", BY_ID["create_server"], {"body": {
                "name": "test", "server_type": "cpx22", "image": "ubuntu-24.04"}})
        self.assertEqual(str(requests[0].url), "https://api.hetzner.cloud/v1/servers")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-token")
        self.assertEqual(json.loads(requests[0].content)["name"], "test")

    async def test_error_does_not_echo_token_or_request(self):
        def handler(request):
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "secret"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(CloudError) as error:
                await Cloud(client).execute("secret", BY_ID["list_servers"], {})
        self.assertNotIn("secret", str(error.exception))
        self.assertIn("429", str(error.exception))

    async def test_timeout_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("secret token", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(CloudError) as error:
                await Cloud(client).execute("secret", BY_ID["delete_server"], {"path": {"id": 42}})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret", str(error.exception))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, Fernet.generate_key().decode())
        self.first = self.store.add(100, "first", "token-one")
        self.second = self.store.add(100, "second", "token-two")
        self.cloud = SimpleNamespace(execute=AsyncMock(return_value={"action": {"id": 7, "status": "running"}}))
        self.manager = Manager(self.store, self.cloud, {100, 200})
        self.context = SimpleNamespace(user_data={})

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_rejects_unauthorized_and_group_messages(self):
        for event in (update(owner=999), update(private=False)):
            await self.manager.projects(event, self.context)
            await self.manager.text(event, self.context)
            event.effective_message.reply_text.assert_not_awaited()
        self.cloud.execute.assert_not_awaited()

    async def test_button_is_bound_to_project_and_requires_confirmation(self):
        event = update(callback=f"quick:{self.first}:42:del")
        await self.manager.callback(event, self.context)
        self.cloud.execute.assert_not_awaited()
        pending = self.context.user_data["pending"]
        self.assertEqual(pending.project, self.first)
        self.assertEqual(pending.payload, {"path": {"id": 42}})
        confirmed = update(callback=f"yes:{pending.nonce}")
        await self.manager.callback(confirmed, self.context)
        self.cloud.execute.assert_awaited_once_with("token-one", BY_ID["delete_server"], {"path": {"id": 42}})
        with self.assertRaises(ValueError):
            await self.manager.callback(confirmed, self.context)

    async def test_switching_project_invalidates_confirmation(self):
        await self.manager.callback(update(callback=f"quick:{self.first}:42:del"), self.context)
        nonce = self.context.user_data["pending"].nonce
        await self.manager.callback(update(callback=f"p:{self.second}"), self.context)
        with self.assertRaises(ValueError):
            await self.manager.callback(update(callback=f"yes:{nonce}"), self.context)
        self.cloud.execute.assert_not_awaited()

    async def test_other_owner_cannot_open_project(self):
        with self.assertRaises(ValueError):
            await self.manager.callback(update(owner=200, callback=f"p:{self.first}"), self.context)
        self.cloud.execute.assert_not_awaited()

    async def test_json_mutation_does_not_execute_until_confirmed(self):
        index = OPERATIONS.index(BY_ID["create_server"])
        await self.manager.ask_operation(update(), self.context, self.second, index)
        await self.manager.text(update(text=json.dumps({"body": {
            "name": "new", "server_type": "cpx22", "image": "ubuntu-24.04"}})), self.context)
        self.assertEqual(self.context.user_data["pending"].project, self.second)
        self.cloud.execute.assert_not_awaited()

    async def test_list_operation_allows_pagination_input(self):
        await self.manager.ask_operation(update(), self.context, self.first,
                                         OPERATIONS.index(BY_ID["list_servers"]))
        self.cloud.execute.assert_not_awaited()
        await self.manager.text(update(text='{"query":{"page":2}}'), self.context)
        self.cloud.execute.assert_awaited_once_with("token-one", BY_ID["list_servers"], {"query": {"page": 2}})

    async def test_secret_response_requires_explicit_single_use_reveal(self):
        self.cloud.execute.return_value = {"root_password": "private-password"}
        event = update()
        await self.manager.execute(event, self.context, self.first, BY_ID["get_server"], {"path": {"id": 42}})
        self.assertNotIn("private-password", str(event.effective_message.reply_text.call_args_list))
        receipt = self.context.user_data["receipt"]
        revealed = update(callback=f"reveal:{receipt['nonce']}")
        await self.manager.callback(revealed, self.context)
        self.assertIn("private-password", str(revealed.effective_message.reply_text.call_args_list))
        with self.assertRaises(ValueError):
            await self.manager.callback(revealed, self.context)

    async def test_expired_secret_response_is_not_revealed(self):
        self.context.user_data["receipt"] = {"nonce": "nonce", "expires": 0,
                                             "value": {"password": "private"}, "project": self.first}
        event = update(callback="reveal:nonce")
        with self.assertRaises(ValueError):
            await self.manager.callback(event, self.context)
        event.effective_message.reply_text.assert_not_awaited()
        self.assertNotIn("receipt", self.context.user_data)

    async def test_json_document_uses_same_confirmation_flow(self):
        await self.manager.ask_operation(update(), self.context, self.first,
                                         OPERATIONS.index(BY_ID["delete_server"]))
        event = update()
        raw = b'{"path":{"id":42}}'
        remote_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(raw)))
        event.effective_message.document = SimpleNamespace(file_size=len(raw),
                                                            get_file=AsyncMock(return_value=remote_file))
        await self.manager.document(event, self.context)
        self.cloud.execute.assert_not_awaited()
        self.assertEqual(self.context.user_data["pending"].payload, {"path": {"id": 42}})
        event.effective_message.delete.assert_awaited_once()

    async def test_token_input_deleted_before_validation_and_stored_encrypted(self):
        self.cloud.execute.return_value = {"servers": []}
        self.context.user_data["input"] = {"kind": "token", "name": "third", "expires": time.monotonic() + 60}
        event = update(text="a" * 64)
        await self.manager.text(event, self.context)
        event.effective_message.delete.assert_awaited_once()
        self.assertEqual(len(self.store.list(100)), 3)
        self.assertNotIn(b"a" * 64, self.store.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
