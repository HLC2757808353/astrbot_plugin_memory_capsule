import os
import re

_version_cache = None


def get_plugin_version():
    global _version_cache

    if _version_cache is not None:
        return _version_cache

    try:
        metadata_path = os.path.join(os.path.dirname(__file__), "..", "metadata.yaml")

        if os.path.exists(metadata_path):
            with open(metadata_path, 'r', encoding='utf-8') as f:
                content = f.read()
            match = re.search(r'^version:\s*(.+)$', content, re.MULTILINE)
            if match:
                _version_cache = match.group(1).strip()
                return _version_cache
    except Exception:
        pass

    _version_cache = 'v0.0.0'
    return _version_cache


def clear_version_cache():
    global _version_cache
    _version_cache = None
