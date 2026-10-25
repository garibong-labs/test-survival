"""Credentials stay private to the model boundary and are never returned in logs."""
import os
from pathlib import Path


def _read(config):
    value = os.environ.get(config['env_key'])
    if value:
        return value
    path = Path(config['env_file'])
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix('export ')
        name, separator, value = line.partition('=')
        if separator and name.strip() == config['env_key']:
            return value.strip().strip('\"\'') or None
    return None


def key_available(config):
    return bool(_read(config))
