import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import httpx

from bot.i18n import LANGUAGE
from bot.main import Manager
from bot.pricing import ExchangeRates, PriceDisplay, RateUnavailable
from bot.storage import Store
from test_bot import update

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)


def market(now=NOW, unit='IRT', price='100000'):
    return {'api': 'nerkhara/v1', 'generated_utc': now.isoformat(), 'item': {
        'id': 'eur', 'family': 'currency', 'unit': unit, 'price': price,
        'updated_utc': now.isoformat(), 'stale': False}}


class RateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.language = LANGUAGE.set('fa')
        self.now = NOW
        self.tick = 100.0
        self.payload = market()
        self.requests = []
        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.payload)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.rates = ExchangeRates(self.client, now=lambda: self.now, monotonic=lambda: self.tick)

    async def asyncTearDown(self):
        LANGUAGE.reset(self.language)
        await self.client.aclose()

    async def test_market_toman_is_converted_to_rial_exactly_and_attributed(self):
        quote = await self.rates.quote('EUR', 'IRR')
        self.assertEqual(quote.factor, Decimal('1000000'))
        self.assertIn('https://nerkhara.com', quote.note())
        self.assertIn('2026-11-18 15:30 Asia/Tehran', quote.note())

    async def test_explicit_rial_unit_is_not_multiplied_by_ten_again(self):
        self.payload = market(unit='IRR', price='1000000')
        self.assertEqual((await self.rates.quote('EUR', 'IRR')).factor, Decimal('1000000'))

    async def test_previous_day_market_rate_is_rejected_even_when_generated_today(self):
        self.payload['item']['updated_utc'] = (NOW-timedelta(days=1)).isoformat()
        with self.assertRaises(RateUnavailable):
            await self.rates.quote('EUR', 'IRR')

    async def test_invalid_market_fields_never_produce_a_price(self):
        cases = [('unit', 'USD'), ('stale', True), ('stale', None), ('id', 'usd'),
                 ('price', 'NaN'), ('price', 'Infinity'), ('price', '-2'), ('price', '0'),
                 ('updated_utc', 42), ('updated_utc', 'invalid'),
                 ('updated_utc', (NOW+timedelta(minutes=10)).isoformat())]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.payload = market()
                self.payload['item'][field] = value
                rates = ExchangeRates(self.client, now=lambda: NOW)
                with self.assertRaises(RateUnavailable):
                    await rates.quote('EUR', 'IRR')

    async def test_reference_conversion_checks_currency_pair_and_date(self):
        self.payload = {'base': 'USD', 'quote': 'EUR', 'rate': '0.9', 'date': '2026-11-18'}
        quote = await self.rates.quote('USD', 'EUR')
        self.assertEqual(quote.factor, Decimal('0.9'))
        self.assertIn('Frankfurter', quote.note())
        self.assertIn('2026-11-18', quote.note())
        self.assertNotIn('00:00', quote.note())
        for changes in ({'quote': 'USD'}, {'date': '2026-11-19'}, {'date': '2026-10-01'}):
            self.payload = {'base': 'USD', 'quote': 'EUR', 'rate': '0.9', 'date': '2026-11-18'}
            self.payload.update(changes)
            with self.assertRaises(RateUnavailable):
                await ExchangeRates(self.client, now=lambda: NOW).quote('USD', 'EUR')

    async def test_valid_rate_is_cached_but_cannot_cross_tehran_midnight(self):
        self.now = datetime(2026, 11, 18, 20, 29, tzinfo=timezone.utc)
        self.payload = market(self.now)
        await self.rates.quote('EUR', 'IRR')
        await self.rates.quote('EUR', 'IRR')
        self.assertEqual(len(self.requests), 1)
        self.now += timedelta(minutes=2)
        self.tick += 120
        with self.assertRaises(RateUnavailable):
            await self.rates.quote('EUR', 'IRR')
        self.assertEqual(len(self.requests), 2)

    async def test_cache_expiry_fetches_a_new_rate(self):
        await self.rates.quote('EUR', 'IRR')
        self.tick += 601
        self.payload['item']['price'] = '200000'
        quote = await self.rates.quote('EUR', 'IRR')
        self.assertEqual(quote.factor, Decimal('2000000'))
        self.assertEqual(len(self.requests), 2)

    async def test_public_request_contains_no_account_data_or_authorization(self):
        await self.rates.quote('EUR', 'IRR')
        request = self.requests[0]
        self.assertEqual(str(request.url), 'https://nerkhara.com/api/v1/latest/eur.json')
        self.assertNotIn('authorization', request.headers)
        self.assertIn('Hetzner-Manager-Bot', request.headers['user-agent'])

    async def test_http_failure_backs_off_instead_of_retrying_in_a_loop(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(503)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            rates = ExchangeRates(client, now=lambda: NOW)
            for _ in range(2):
                with self.assertRaises(RateUnavailable):
                    await rates.quote('EUR', 'IRR')
        self.assertEqual(len(calls), 1)

    async def test_redirect_is_not_followed(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(302, headers={'Location': 'https://example.com/'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
            with self.assertRaises(RateUnavailable):
                await ExchangeRates(client, now=lambda: NOW).quote('EUR', 'IRR')
        self.assertEqual(len(calls), 1)


class DisplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.language = LANGUAGE.set('fa')
        self.directory = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.store = Store(self.directory.name, self.key)
        self.pid = self.store.add(100, 'production', 'private-token')
        self.payload = market()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=self.payload)))
        self.rates = ExchangeRates(self.client, now=lambda: NOW)
        self.display = PriceDisplay(self.store, self.rates)

    async def asyncTearDown(self):
        LANGUAGE.reset(self.language)
        self.directory.cleanup()
        await self.client.aclose()

    async def test_preference_is_per_user_and_survives_restart_with_language_intact(self):
        self.store.set_display_currency(100, 'IRR')
        self.store.set_display_currency(200, 'EUR')
        self.store.set_language(100, 'en')
        reopened = Store(self.directory.name, self.key)
        self.assertEqual(reopened.display_currency(100), 'IRR')
        self.assertEqual(reopened.display_currency(200), 'EUR')
        self.assertIsNone(reopened.display_currency(300))
        self.assertEqual(reopened.languages(), {100: 'en'})
        self.assertEqual(reopened.get(100, self.pid)[1], 'private-token')

    async def test_legacy_default_keeps_native_currency_and_two_decimals(self):
        context = await self.display.context(100, 'USD')
        self.assertEqual(context.format('4.995'), '5.00 USD')
        self.assertIsNone(context.quote)

    async def test_euro_account_in_euro_mode_requires_no_exchange_request(self):
        self.store.set_display_currency(100, 'EUR')
        rates = SimpleNamespace(quote=AsyncMock(side_effect=AssertionError('unexpected request')))
        context = await PriceDisplay(self.store, rates).context(100, 'EUR')
        self.assertEqual(context.format('4'), '4.00 EUR')
        rates.quote.assert_not_awaited()

    async def test_rial_conversion_uses_unrounded_source_price_and_two_decimal_places(self):
        self.store.set_display_currency(100, 'IRR')
        context = await self.display.context(100, 'EUR')
        self.assertEqual(context.format('4.995'), '≈ 4,995,000.00 ریال')
        self.assertIn('NerkhAra', context.note)

    async def test_dollar_account_can_be_displayed_in_euro(self):
        self.store.set_display_currency(100, 'EUR')
        self.payload = {'base': 'USD', 'quote': 'EUR', 'rate': '0.9', 'date': '2026-11-18'}
        context = await self.display.context(100, 'USD')
        self.assertEqual(context.format('10'), '≈ 9.00 EUR')

    async def test_unavailable_rate_falls_back_to_native_unit_with_explanation(self):
        self.store.set_display_currency(100, 'IRR')
        rates = SimpleNamespace(quote=AsyncMock(side_effect=RateUnavailable))
        context = await PriceDisplay(self.store, rates).context(100, 'USD')
        self.assertEqual(context.format('4'), '4.00 USD')
        self.assertIn('USD', context.note)
        self.assertIsNone(context.quote)

    async def test_currency_settings_save_and_cancel_old_confirmation(self):
        manager = Manager(self.store, SimpleNamespace(execute=AsyncMock()), {100}, self.rates)
        context = SimpleNamespace(user_data={'pending': object()})
        event = update(callback='currency:IRR')
        await manager.callback(event, context)
        self.assertEqual(self.store.display_currency(100), 'IRR')
        self.assertEqual(context.user_data, {})
        text = event.effective_message.reply_text.call_args.args[0]
        self.assertIn('https://nerkhara.com', text)
        self.assertIn('2026-11-18', text)

    async def test_unauthorized_user_or_group_cannot_change_currency(self):
        manager = Manager(self.store, SimpleNamespace(execute=AsyncMock()), {100}, self.rates)
        for event in (update(owner=999, callback='currency:IRR'), update(private=False, callback='currency:IRR')):
            await manager.callback(event, SimpleNamespace(user_data={}))
        self.assertIsNone(self.store.display_currency(100))
        self.assertIsNone(self.store.display_currency(999))

    async def test_server_card_uses_the_selected_currency_and_source(self):
        self.store.set_display_currency(100, 'IRR')
        cloud = SimpleNamespace(execute=AsyncMock(return_value={'pricing': {'currency': 'EUR'}}))
        manager = Manager(self.store, cloud, {100}, self.rates)
        server = {'id': 42, 'name': 'web', 'status': 'off', 'datacenter': {'location': {'name': 'fsn1'}},
                  'server_type': {'name': 'cx23', 'prices': [{'location': 'fsn1', 'price_monthly': {'gross': '4'}}]}}
        text = await manager.server_text(100, 'production', 'private-token', server)
        self.assertIn('4,000,000.00 ریال', text)
        self.assertIn('https://nerkhara.com', text)

    async def test_plan_conversion_is_display_only_and_source_is_present_at_confirmation(self):
        from test_simple import PLAN, SERVER
        self.store.set_display_currency(100, 'IRR')
        async def native(token, operation, payload):
            name = operation['id']
            if name == 'list_server_types':
                return {'server_types': [copy.deepcopy(PLAN)]}
            if name == 'list_locations':
                return {'locations': [{'id': 1, 'name': 'fsn1', 'city': 'Falkenstein', 'country': 'DE'}]}
            if name == 'get_pricing':
                return {'pricing': {'currency': 'EUR'}}
            if name == 'list_images':
                return {'images': [{'id': 10, 'name': 'ubuntu', 'architecture': 'x86'}]}
            if name == 'list_ssh_keys':
                return {'ssh_keys': []}
            return {'server': SERVER}
        cloud = SimpleNamespace(execute=AsyncMock(side_effect=native))
        manager = Manager(self.store, cloud, {100}, self.rates)
        context = SimpleNamespace(user_data={})
        await manager.simple.start(update(), context, self.pid, 'create_server')
        await manager.text(update(text='my-server'), context)
        async def click(action):
            nonce = context.user_data['input']['nonce']
            event = update(callback=f'easy:{nonce}:{action}')
            await manager.callback(event, context)
            return event
        plan_screen = await click('pick:0')
        self.assertIn('https://nerkhara.com', plan_screen.effective_message.reply_text.call_args.args[0])
        self.assertIn('ریال', context.user_data['input']['options'][0][1])
        await click('pick:0')
        await click('pick:0')
        self.payload['item']['price'] = '200000'
        self.rates.cache.clear()
        confirmation = await click('skip')
        self.assertIn('https://nerkhara.com', confirmation.effective_message.reply_text.call_args.args[0])
        self.assertIn('9,980,000.00', confirmation.effective_message.reply_text.call_args.args[0])
        self.assertEqual(context.user_data['pending'].payload, {'body': {
            'name': 'my-server', 'location': 'fsn1', 'server_type': PLAN['name'], 'image': '10'}})
        self.assertTrue(all(call.args[1]['method'] == 'GET' for call in cloud.execute.call_args_list))
