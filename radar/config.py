"""Configuration is local and contains only a key file reference, never a key."""
import json
import math
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = Path.home() / 'Library' / 'Application Support' / 'SecondaryBrainRadar'
DEFAULT_CONFIG = ROOT / 'radar' / 'config.local.json'
EXAMPLE = ROOT / 'radar' / 'config.example.json'
ADDRESS = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def load(path=DEFAULT_CONFIG):
    config = json.loads(EXAMPLE.read_text())
    if Path(path).exists():
        config.update(json.loads(Path(path).read_text()))
    if config['mode'] != 'paper' or config['chain'] != 'solana':
        raise ValueError('Only Solana paper mode is implemented; no live trading exists.')
    for key, value in config.items():
        if key.startswith('_'):
            continue
        if key in ('brain_sync', 'wallets', 'chain', 'mode', 'key_file', 'key_variable'):
            continue
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'Invalid nonnegative number: {key}')
    for key in ('loop_seconds', 'token_interval_seconds', 'smart_interval_seconds', 'wallet_interval_seconds', 'quote_max_age_seconds'):
        if config[key] < 30:
            raise ValueError(f'{key} must be >= 30')
    if not 1 <= config['max_candidates'] <= 300 or not 0 <= config['max_positions'] <= 20:
        raise ValueError('Invalid candidate / position limit')
    for key in ('slippage_fraction', 'fee_fraction', 'stop_loss_fraction'):
        if config[key] >= 1:
            raise ValueError(f'{key} must be < 1')
    if config['reserve_usd'] > config['initial_cash_usd']:
        raise ValueError('Reserve exceeds starting cash')
    if not isinstance(config['wallets'], list) or any(not ADDRESS.fullmatch(a) for a in config['wallets']):
        raise ValueError('wallets must contain Solana addresses')
    for key in ('max_candidates', 'max_positions'):
        if not isinstance(config[key], int):
            raise ValueError(f'{key} must be an integer')
    if not isinstance(config['brain_sync'], bool):
        raise ValueError('brain_sync must be boolean')
    if not isinstance(config['key_variable'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', config['key_variable']):
        raise ValueError('Invalid key variable name')
    if not isinstance(config['key_file'], str):
        raise ValueError('key_file must be a path')
    config['wallets'] = list(dict.fromkeys(config['wallets']))
    return config


def read_key(config):
    """Read a named env assignment or plain key from a private user-owned file."""
    variable = config['key_variable']
    if os.environ.get(variable):
        return os.environ[variable].strip()
    if not config.get('key_file'):
        return None
    path = Path(config['key_file']).expanduser()
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('Key file must be user-owned and mode 600 (chmod 600 FILE).')
    raw = path.read_text().strip()
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[7:]
        if line.startswith(variable + '='):
            return line.split('=', 1)[1].strip().strip('\"\'') or None
    if '\n' not in raw and '=' not in raw and raw and not raw.startswith('#'):
        return raw
    return None
