"""Per-handler language context; translate message templates before inserting user data."""
from contextvars import ContextVar
import json
from pathlib import Path

LANGUAGE = ContextVar('bot_language', default='fa')
ENGLISH = json.loads(Path(__file__).with_name('en.json').read_text(encoding='utf-8'))


def tr(source, *values):
    template = ENGLISH.get(source, source) if LANGUAGE.get() == 'en' else source
    if not template:
        template = source
    return template.format(*values) if values else template
