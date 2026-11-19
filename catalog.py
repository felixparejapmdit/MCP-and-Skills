"""Versioned public default catalog. Machine inventory never defines defaults."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

AGENTS = ('claude', 'codex', 'gemini')
PLATFORMS = ('windows', 'macos', 'linux')
NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,95}$')
SHA = re.compile(r'^[0-9a-f]{40}$')


def load_catalog(app_dir=None):
    path = Path(app_dir or Path(__file__).parent) / 'catalog.json'
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('schemaVersion') != 1 or not isinstance(value.get('items'), list):
        raise ValueError('The default catalog has an unsupported schema')
    seen = set()
    for item in value['items']:
        kind, name = item.get('kind'), item.get('name')
        key = item.get('id', '')
        if kind not in {'mcp', 'skill'} or not NAME.fullmatch(str(name)):
            raise ValueError('The catalog has an invalid item identity')
        if key != kind + ':' + name or key in seen:
            raise ValueError('The catalog has duplicate or mismatched identities')
        seen.add(key)
        if not set(item.get('supportedAgents', [])) <= set(AGENTS):
            raise ValueError('The catalog has an unknown agent')
        if not set(item.get('supportedPlatforms', [])) <= set(PLATFORMS):
            raise ValueError('The catalog has an unknown operating system')
        recipe = item.get('recipe', {})
        if item.get('installable') and recipe.get('type') == 'github':
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', recipe.get('repo', '')):
                raise ValueError('The catalog has an invalid publisher')
            if not SHA.fullmatch(recipe.get('ref', '')):
                raise ValueError('The catalog skill revision is not pinned')
            parts = recipe.get('path', '').replace('\\', '/').split('/')
            if not parts or any(p in {'', '.', '..'} or ':' in p for p in parts):
                raise ValueError('The catalog source path is unsafe')
        if item.get('installable') and recipe.get('type') not in {'http', 'npm', 'python', 'github'}:
            raise ValueError('The catalog has an unsupported install recipe')
    return copy.deepcopy(value)


def catalog_items(app_dir=None):
    return {item['id']: item for item in load_catalog(app_dir)['items']}
