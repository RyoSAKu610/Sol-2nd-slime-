"""HTTP adapters. API credentials can only travel to api.nansen.ai over HTTPS."""
from datetime import datetime, timezone
import errno
import http.client
import json
import math
import socket
import ssl
import urllib.error
import urllib.request

NANSEN = 'https://api.nansen.ai'
DEX = 'https://api.dexscreener.com'


class FeedError(Exception):
    def __init__(self, message, status=0, retry_after=None, category=None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after
        self.category = category


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward API credentials through redirects.
        raise FeedError('HTTP redirect rejected', code)


def _network_failure(exc):
    """Keep actionable categories, never exception text, URLs, or response bodies."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    code = getattr(reason, 'errno', None)
    if isinstance(reason, socket.gaierror):
        category = 'dns'
    elif isinstance(reason, ssl.SSLCertVerificationError):
        category = 'tls_certificate'
    elif isinstance(reason, ssl.SSLError):
        category = 'tls'
    elif isinstance(reason, TimeoutError) or code == errno.ETIMEDOUT:
        category = 'timeout'
    elif isinstance(reason, ConnectionRefusedError) or code == errno.ECONNREFUSED:
        category = 'connection_refused'
    elif isinstance(reason, ConnectionResetError) or code == errno.ECONNRESET:
        category = 'connection_reset'
    elif code in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        category = 'network_unreachable'
    elif isinstance(reason, OSError):
        category = 'os_error'
    elif isinstance(reason, http.client.HTTPException):
        category = 'http_protocol'
    else:
        category = 'unknown_network'
    details = []
    for label in ('errno', 'verify_code'):
        value = getattr(reason, label, None)
        if type(value) is int:
            details.append(f'{label}={value}')
    message = 'Network failure: ' + category + (' (' + ', '.join(details) + ')' if details else '')
    return FeedError(message, category=category)


def request(url, payload=None, key=None):
    if key and not url.startswith(NANSEN + '/api/'):
        raise ValueError('Credential destination rejected')
    headers = {'Accept': 'application/json', 'User-Agent': 'SecondaryBrainRadar/1.0'}
    if key:
        headers['apikey'] = key
    body = None if payload is None else json.dumps(payload).encode()
    if body is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, headers=headers, method='GET' if payload is None else 'POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise FeedError('Response exceeded 8 MB')
            return json.loads(raw), dict(response.headers)
    except urllib.error.HTTPError as exc:
        # Do not print echoed server bodies: they can contain credential or request values.
        retry = exc.headers.get('Retry-After')
        raise FeedError(f'HTTP {exc.code}', exc.code, int(retry) if retry and retry.isdigit() else None) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
        raise _network_failure(exc) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FeedError('Response was not valid JSON') from None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def timestamp(value):
    if isinstance(value, (int, float)):
        value = number(value)
        if value is None:
            return None
        return value / 1000 if abs(value) > 100_000_000_000 else value
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def rows(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise FeedError('Unexpected response schema: expected data array')
    return [row for row in payload['data'] if isinstance(row, dict)]


def nansen_specs(config, now, wallet=None):
    common = {'chains': ['solana'], 'pagination': {'page': 1, 'per_page': 50}}
    specs = [
        ('account', '/api/v1/account', None, 0, 86400),
        ('tokens', '/api/v1/token-screener', dict(common, timeframe='1h',
            filters={'trader_type': 'sm', 'include_stablecoins': False, 'include_native_tokens': False,
                     'liquidity': {'min': config['min_liquidity_usd']}},
            order_by=[{'field': 'netflow', 'direction': 'DESC'}]), 1, config['token_interval_seconds']),
        ('netflow', '/api/v1/smart-money/netflow', dict(common,
            filters={'include_stablecoins': False, 'include_native_tokens': False},
            order_by=[{'field': 'net_flow_1h_usd', 'direction': 'DESC'}]), 5, config['smart_interval_seconds']),
        ('dex-trades', '/api/v1/smart-money/dex-trades', dict(common,
            order_by=[{'field': 'block_timestamp', 'direction': 'DESC'}]), 5, config['smart_interval_seconds']),
    ]
    if wallet:
        # V1 requires a date range. Revisit the trailing day and deduplicate by
        # transaction identity in storage; this is not a complete wallet history.
        date = {'from': datetime.fromtimestamp(now - 86400, timezone.utc).isoformat(),
                'to': datetime.fromtimestamp(now, timezone.utc).isoformat()}
        specs.append(('wallet', '/api/v1/profiler/address/transactions',
                      {'address': wallet, 'chain': 'solana', 'date': date,
                       'pagination': {'page': 1, 'per_page': 25},
                       'order_by': [{'field': 'block_timestamp', 'direction': 'DESC'}]},
                      1, config['wallet_interval_seconds']))
    return specs


def _mapping(value):
    return value if isinstance(value, dict) else {}


def quote_for(address, pairs, now):
    if not isinstance(pairs, list):
        raise FeedError('Unexpected response schema: expected pairs array')
    eligible = [p for p in pairs if isinstance(p, dict) and p.get('chainId') == 'solana'
                and _mapping(p.get('baseToken')).get('address') == address
                and (number(p.get('priceUsd')) or 0) > 0]
    if not eligible:
        return None
    p = max(eligible, key=lambda p: number(_mapping(p.get('liquidity')).get('usd')) or 0)
    tx = _mapping(_mapping(p.get('txns')).get('m5'))
    # API supplies no last-price-update time. pairCreatedAt is NOT price time.
    return {'chain': 'solana', 'address': address, 'pair_address': p.get('pairAddress'),
            'symbol': str(_mapping(p.get('baseToken')).get('symbol') or address[:8])[:100],
            'price_usd': number(p.get('priceUsd')), 'liquidity_usd': number(_mapping(p.get('liquidity')).get('usd')),
            'volume_5m_usd': number(_mapping(p.get('volume')).get('m5')),
            'buys_5m': number(tx.get('buys')), 'sells_5m': number(tx.get('sells')),
            'change_5m_pct': number(_mapping(p.get('priceChange')).get('m5')),
            'pair_created_at': timestamp(p.get('pairCreatedAt')), 'observed_at': now,
            'source_price_updated_at': None, 'source': DEX + '/tokens/v1/solana/' + address,
            'url': 'https://dexscreener.com/solana/' + str(p.get('pairAddress') or address)}
