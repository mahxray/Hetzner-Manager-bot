"""Current per-location capacity rules shared by simple and advanced plan pickers."""
from .i18n import tr
from datetime import datetime, timezone


def available_plan(plan, location=None, now=None):
    now = now or datetime.now(timezone.utc)
    for entry in plan.get('locations', []):
        if location is not None and entry.get('name') != location:
            continue
        if entry.get('available') is not True:
            continue
        removal = (entry.get('deprecation') or {}).get('unavailable_after')
        if removal:
            try:
                if datetime.fromisoformat(removal.replace('Z', '+00:00')) <= now:
                    continue
            except (ValueError, TypeError):
                continue
        return True
    return False


async def all_items(cloud, token, operation, key, query=None):
    result = []
    for page in range(1, 101):
        response = await cloud.execute(token, operation,
                                       {'query': {**(query or {}), 'page': page, 'per_page': 50}})
        result.extend(response[key])
        if not response.get('meta', {}).get('pagination', {}).get('next_page'):
            return result
    raise ValueError(tr('فهرست منابع بیش از حد بزرگ است؛ از بخش پیشرفته با فیلتر استفاده کنید.'))
