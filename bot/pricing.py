"""Public FX data has a separate client/cache and never receives Hetzner credentials."""
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
import re
import time
from zoneinfo import ZoneInfo

import httpx

from .i18n import tr

TEHRAN = ZoneInfo('Asia/Tehran')


class RateUnavailable(Exception):
    pass


def positive(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('Invalid exchange rate')
    return result


@dataclass(frozen=True)
class Quote:
    base: str
    target: str
    factor: Decimal
    observed: datetime | date
    provider: str

    def current(self, now):
        if self.target == 'IRR':
            return (isinstance(self.observed, datetime)
                    and self.observed.astimezone(TEHRAN).date() == now.astimezone(TEHRAN).date()
                    and -300 <= (now - self.observed).total_seconds() <= 86400)
        observed = self.observed.date() if isinstance(self.observed, datetime) else self.observed
        return 0 <= (now.date() - observed).days <= 7

    def note(self):
        unit = tr('ریال') if self.target == 'IRR' else self.target
        rate = format(self.factor.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), ',.2f')
        stamp = (self.observed.astimezone(TEHRAN).strftime('%Y-%m-%d %H:%M Asia/Tehran')
                 if isinstance(self.observed, datetime) else self.observed.isoformat())
        return tr('نرخ: 1 {0} = {1} {2}\nتاریخ/زمان نرخ: {4} · {3}\nقیمت تبدیل‌شده تخمینی است.',
                  self.base, rate, unit, self.provider, stamp)


class ExchangeRates:
    def __init__(self, client, now=None, monotonic=None):
        self.client = client
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.cache = {}
        self.retry_after = {}
        self.lock = asyncio.Lock()

    async def quote(self, base, target):
        if not isinstance(base, str) or not re.fullmatch('[A-Z]{3}', base) or target not in {'IRR', 'EUR'}:
            raise RateUnavailable
        async with self.lock:
            now = self.now()
            cached = self.cache.get((base, target))
            if cached and cached[1] > self.monotonic() and cached[0].current(now):
                return cached[0]
            if self.retry_after.get((base, target), 0) > self.monotonic():
                raise RateUnavailable
            try:
                # No tokens, user identifiers, project data, or request headers are copied here.
                headers = {'User-Agent': 'Hetzner-Manager-Bot/1.0', 'Accept': 'application/json'}
                if target == 'IRR':
                    response = await self.client.get(f'https://nerkhara.com/api/v1/latest/{base.lower()}.json',
                                                     headers=headers, timeout=10, follow_redirects=False)
                    response.raise_for_status()
                    data = response.json(parse_float=Decimal)
                    item = data['item']
                    if (data['api'] != 'nerkhara/v1' or item['id'] != base.lower()
                            or item['family'] != 'currency' or item['stale'] is not False
                            or item['unit'] not in {'IRT', 'IRR'}):
                        raise ValueError('Invalid market quote')
                    factor = positive(item['price']) * (10 if item['unit'] == 'IRT' else 1)
                    if not isinstance(item['updated_utc'], str):
                        raise ValueError('Invalid observation timestamp')
                    observed = datetime.fromisoformat(item['updated_utc'].replace('Z', '+00:00'))
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    quote = Quote(base, 'IRR', factor, observed, 'NerkhAra — https://nerkhara.com')
                else:
                    response = await self.client.get(f'https://api.frankfurter.dev/v2/rate/{base.lower()}/eur',
                                                     headers=headers, timeout=10, follow_redirects=False)
                    response.raise_for_status()
                    data = response.json(parse_float=Decimal)
                    if data['base'] != base or data['quote'] != 'EUR':
                        raise ValueError('Invalid reference quote')
                    quote = Quote(base, 'EUR', positive(data['rate']), date.fromisoformat(data['date']),
                                  'Frankfurter — https://frankfurter.dev')
                if not quote.current(now):
                    raise ValueError('Stale quote')
                quote.factor.quantize(Decimal('0.01'))
            except (httpx.HTTPError, ValueError, TypeError, KeyError, InvalidOperation):
                self.cache.pop((base, target), None)
                self.retry_after[(base, target)] = self.monotonic() + 30
                logging.warning('fx_quote_unavailable base=%s target=%s', base, target)
                raise RateUnavailable from None
            self.cache[(base, target)] = (quote, self.monotonic() + 600)
            self.retry_after.pop((base, target), None)
            return quote


@dataclass(frozen=True)
class PriceContext:
    currency: str
    factor: Decimal = Decimal(1)
    quote: Quote | None = None
    note: str = ''

    def format(self, amount):
        value = Decimal(str(amount)) * self.factor
        if not value.is_finite() or value < 0:
            raise ValueError(tr('قیمت دریافت‌شده معتبر نیست.'))
        value = value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        number = format(value, ',.2f' if self.currency == 'IRR' else '.2f')
        unit = tr('ریال') if self.currency == 'IRR' else self.currency
        return ('≈ ' if self.quote else '') + f'{number} {unit}'


class PriceDisplay:
    def __init__(self, store, rates=None):
        self.store, self.rates = store, rates

    async def context(self, actor, source):
        target = await asyncio.to_thread(self.store.display_currency, actor)
        if target is None or target == source:
            return PriceContext(source)
        try:
            if self.rates is None:
                raise RateUnavailable
            quote = await self.rates.quote(source, target)
            return PriceContext(target, quote.factor, quote, quote.note())
        except RateUnavailable:
            return PriceContext(source, note=tr('نرخ معتبر در دسترس نیست؛ قیمت اصلی حساب هتزنر با واحد {0} نمایش داده می‌شود.', source))
