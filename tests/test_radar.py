import errno
import http.client
import json
from pathlib import Path
import tempfile
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from radar.config import load
from radar.feeds import FeedError, nansen_specs, quote_for, request, timestamp
from radar.report import render
from radar.runner import Radar, score_quote
from radar.service import Lock, canonical_memory
from radar.store import Store, dumps

ADDRESS = 'So11111111111111111111111111111111111111112'
OTHER = '11111111111111111111111111111111'


def pair(price='1', symbol='TEST'):
    return {'chainId': 'solana', 'baseToken': {'address': ADDRESS, 'symbol': symbol},
            'pairAddress': OTHER, 'priceUsd': price, 'liquidity': {'usd': 100000},
            'volume': {'m5': 10000}, 'txns': {'m5': {'buys': 40, 'sells': 10}},
            'priceChange': {'m5': 10}, 'pairCreatedAt': 1600000000000}


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.now = 1789000000.0
        self.config = load(Path('/nonexistent/config.json'))
        self.store = Store(self.path / 'radar.sqlite3', lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_repeated_observation_aggregates_and_distinct_change_survives(self):
        self.store.success('dex', [{'p': 1}, {'p': 2}], 2, 60)
        self.now += 60
        self.store.success('dex', [{'p': 1}], 1, 60)
        rows = self.store.history()
        self.assertEqual(len(rows), 2)
        repeated = [r for r in rows if json.loads(r['payload'])['p'] == 1][0]
        self.assertEqual(repeated['seen_count'], 2)
        self.assertEqual(repeated['at'] - repeated['first_at'], 60)
        self.now += 30 * 86400
        self.store.cleanup(1)
        self.assertEqual(len(self.store.history()), 2)

    def test_transaction_reenrichment_uses_stable_identity(self):
        self.store.success('wallet', [{'transaction_hash': 'tx1', 'value': 1}], 1, 60)
        self.store.success('wallet', [{'transaction_hash': 'tx1', 'value': 2}], 1, 60)
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(self.store.history()[0]['seen_count'], 2)

    def test_conservative_credit_reservation_survives_restart(self):
        self.config['daily_credits'] = 5
        credit = self.store.reserve('/api/test', 5, self.config)
        self.store.charge(credit, 'timeout')
        other = Store(self.path / 'radar.sqlite3', lambda: self.now)
        try:
            with self.assertRaises(ValueError): other.reserve('/api/test', 1, self.config)
            self.assertEqual(other.credit_totals()['day'], 5)
        finally: other.close()

    def test_budget_zero_makes_no_paid_request(self):
        self.config['daily_credits'] = 0
        with self.assertRaises(ValueError): self.store.reserve('/api/test', 1, self.config)
        self.store.reserve('/api/v1/account', 0, self.config)

    def test_paper_restart_never_mints_cash_and_has_friction(self):
        radar = Radar(self.path, now=lambda: self.now)
        try:
            self.store.token(ADDRESS, 'TEST', 'dex:profile')
            quote = quote_for(ADDRESS, [pair()], self.now)
            with self.store.db:
                self.store.db.execute('UPDATE tokens SET quote=?,quote_at=?', (dumps(quote), self.now))
            radar.simulate(self.config)
            p = radar.store.paper(self.config)
            self.assertEqual(len(p['open']), 1)
            self.assertEqual(p['cash'], 18)
            self.assertLess(p['open'][0]['quantity'], 2)
            radar.simulate(self.config)
            self.assertEqual(len(radar.store.paper(self.config)['open']), 1)
            self.config['initial_cash_usd'] = 500
            self.assertEqual(radar.store.paper(self.config)['initial_cash'], 20)
            self.now += 60
            quote = quote_for(ADDRESS, [pair('0.5')], self.now)
            with self.store.db:
                self.store.db.execute('UPDATE tokens SET quote=?,quote_at=?', (dumps(quote), self.now))
            radar.simulate(self.config)
            p = radar.store.paper(self.config)
            self.assertEqual(len(p['closed']), 1)
            self.assertEqual(p['closed'][0]['reason'], 'stop_loss')
            self.assertGreaterEqual(p['cash'], 0)
            self.assertEqual(len(p['open']), 0)
        finally: radar.close()

    def test_stale_missing_quote_prevents_entry(self):
        q = quote_for(ADDRESS, [pair()], self.now - 121)
        self.assertFalse(score_quote(q, {}, self.config, self.now)[3])
        self.assertFalse(score_quote(None, {}, self.config, self.now)[3])

    def test_source_clock_is_not_pool_creation(self):
        q = quote_for(ADDRESS, [pair()], self.now)
        self.assertEqual(q['observed_at'], self.now)
        self.assertIsNone(q['source_price_updated_at'])
        self.assertNotEqual(q['pair_created_at'], q['observed_at'])
        self.assertIsNone(timestamp(float('nan')))

    def test_nansen_wallet_not_smart_money_signal(self):
        q = quote_for(ADDRESS, [pair()], self.now)
        base = score_quote(q, {}, self.config, self.now)[0]
        self.assertEqual(score_quote(q, {'nansen:wallet:' + OTHER: self.now}, self.config, self.now)[0], base)
        self.assertEqual(score_quote(q, {'nansen:netflow': self.now}, self.config, self.now)[0], base)
        self.assertEqual(score_quote(q, {'nansen:positive:netflow': self.now}, self.config, self.now)[0], base + 20)

    def test_positive_netflow_withdrawn_on_negative_update(self):
        radar = Radar(self.path, now=lambda: self.now)
        try:
            radar.ingest_nansen('nansen:netflow', [{'token_address': ADDRESS, 'net_flow_1h_usd': 12}])
            self.assertIn('positive', radar.store.rows('SELECT sources FROM tokens')[0]['sources'])
            radar.ingest_nansen('nansen:netflow', [{'token_address': ADDRESS, 'net_flow_1h_usd': -1}])
            self.assertNotIn('positive', radar.store.rows('SELECT sources FROM tokens')[0]['sources'])
        finally: radar.close()

    def test_wallet_contract_has_date_and_extracts_received(self):
        spec = nansen_specs(self.config, self.now, OTHER)[-1]
        self.assertEqual(timestamp(spec[2]['date']['to']) - timestamp(spec[2]['date']['from']), 86400)
        radar = Radar(self.path, now=lambda: self.now)
        try:
            radar.ingest_nansen('nansen:wallet:' + OTHER, [{'tokens_received': [{'token_address': ADDRESS, 'token_symbol': 'TEST'}]}])
            self.assertEqual(len(radar.store.rows('SELECT * FROM tokens')), 1)
        finally: radar.close()

    def test_smart_swap_extracts_both_legs_and_wallet_without_buy_boost(self):
        radar = Radar(self.path, now=lambda: self.now)
        try:
            radar.ingest_nansen('nansen:dex-trades', [{'token_bought_address': ADDRESS, 'token_bought_symbol': 'BUY',
                'token_sold_address': OTHER, 'token_sold_symbol': 'SELL', 'trader_address': OTHER}])
            tokens = radar.store.rows('SELECT * FROM tokens')
            self.assertEqual(len(tokens), 2)
            self.assertTrue(all('positive' not in t['sources'] for t in tokens))
            self.assertEqual(len(radar.store.rows('SELECT * FROM wallets')), 1)
        finally: radar.close()

    def test_missing_key_does_not_request_nansen(self):
        called = []
        radar = Radar(self.path, http=lambda *args: called.append(args), now=lambda: self.now)
        try:
            with patch('radar.config.read_key', return_value=None): radar.poll_nansen(self.config)
            self.assertEqual(called, [])
            self.assertEqual(radar.store.feed('nansen')['state'], 'missing_credential')
        finally: radar.close()

    def test_auth_failure_is_not_retried_and_raw_body_unavailable(self):
        calls = []
        def http(*args):
            calls.append(args[0]); raise FeedError('HTTP 401', 401)
        radar = Radar(self.path, http=http, now=lambda: self.now)
        try:
            with patch('radar.config.read_key', return_value='private-key'):
                radar.poll_nansen(self.config); radar.poll_nansen(self.config)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('private-key', dumps(radar.store.rows('SELECT * FROM feeds')))
        finally: radar.close()

    def test_network_error_category_keeps_no_exception_text(self):
        secret = 'private-key https://user:password@example.invalid/path?token=secret'
        cert = ssl.SSLCertVerificationError(1, secret)
        cert.verify_code = 20
        cases = [
            (socket.gaierror(socket.EAI_NONAME, secret), 'dns', 'errno='),
            (cert, 'tls_certificate', 'verify_code=20'),
            (ssl.SSLError(1, secret), 'tls', 'errno=1'),
            (TimeoutError(secret), 'timeout', None),
            (ConnectionRefusedError(errno.ECONNREFUSED, secret), 'connection_refused', 'errno='),
            (ConnectionResetError(errno.ECONNRESET, secret), 'connection_reset', 'errno='),
            (OSError(errno.ENETUNREACH, secret), 'network_unreachable', 'errno='),
            (OSError(errno.EIO, secret), 'os_error', 'errno='),
            (http.client.BadStatusLine(secret), 'http_protocol', None),
            (secret, 'unknown_network', None),
        ]
        for reason, category, detail in cases:
            with self.subTest(category=category):
                with patch('radar.feeds.urllib.request.build_opener') as opener:
                    opener.return_value.open.side_effect = urllib.error.URLError(reason)
                    with self.assertRaises(FeedError) as caught:
                        request('https://api.nansen.ai/api/v1/account', key='private-key')
                message = str(caught.exception)
                self.assertTrue(message.startswith('Network failure: ' + category))
                self.assertEqual(caught.exception.category, category)
                if detail: self.assertIn(detail, message)
                for forbidden in ('private-key', 'password', 'example.invalid', 'token=secret'):
                    self.assertNotIn(forbidden, message)
                self.assertEqual(caught.exception.status, 0)

    def test_direct_network_error_is_persisted_without_changing_backoff(self):
        radar = Radar(self.path, now=lambda: self.now)
        try:
            for delay in (60, 120):
                with patch('radar.feeds.urllib.request.build_opener') as opener:
                    opener.return_value.open.side_effect = TimeoutError('must-not-be-logged')
                    try:
                        request('https://api.dexscreener.com/token-profiles/latest/v1')
                    except FeedError as exc:
                        radar.failed('dex:profiles', exc)
                saved = radar.store.feed('dex:profiles')
                self.assertEqual(saved['last_error'], 'Network failure: timeout')
                self.assertEqual(saved['next_at'] - self.now, delay)
        finally: radar.close()

    def test_dex_dns_backoff_caps_at_five_minutes_and_success_resets(self):
        radar = Radar(self.path, now=lambda: self.now)
        try:
            error = FeedError('Network failure: dns (errno=8)', category='dns')
            for delay in (60, 120, 240, 300, 300, 300, 300, 300):
                radar.failed('dex:quotes:0', error)
                self.assertEqual(radar.store.feed('dex:quotes:0')['next_at'] - self.now, delay)
            radar.store.success('dex:quotes:0', [], 0, 60)
            self.assertEqual(radar.store.feed('dex:quotes:0')['failures'], 0)
            radar.failed('dex:quotes:0', error)
            self.assertEqual(radar.store.feed('dex:quotes:0')['next_at'] - self.now, 60)
        finally: radar.close()

    def test_other_errors_and_paid_feed_dns_keep_original_hour_cap(self):
        radar = Radar(self.path, now=lambda: self.now)
        cases = [('nansen:tokens', FeedError('Network failure: dns', category='dns')),
                 ('dex:quotes:0', FeedError('Network failure: tls', category='tls')),
                 ('dex:quotes:1', FeedError('Network failure: timeout', category='timeout')),
                 ('dex:profiles', FeedError('HTTP 429', 429))]
        try:
            for name, error in cases:
                with self.subTest(feed=name, category=error.category):
                    for delay in (60, 120, 240, 480, 960, 1920, 3600, 3600):
                        radar.failed(name, error)
                        self.assertEqual(radar.store.feed(name)['next_at'] - self.now, delay)
        finally: radar.close()

    def test_explicit_retry_after_remains_authoritative(self):
        radar = Radar(self.path, now=lambda: self.now)
        try:
            radar.failed('dex:quotes:0', FeedError('Network failure: dns', retry_after=900, category='dns'))
            self.assertEqual(radar.store.feed('dex:quotes:0')['next_at'] - self.now, 900)
            radar.failed('dex:profiles', FeedError('HTTP 429', 429, retry_after=7200))
            self.assertEqual(radar.store.feed('dex:profiles')['next_at'] - self.now, 7200)
        finally: radar.close()

    def test_origin_guard_and_malformed_quote(self):
        with self.assertRaises(ValueError): request('https://evil.example/api/v1', key='secret')
        malformed = pair(); malformed['liquidity'] = 'bad'; malformed['txns'] = []
        q = quote_for(ADDRESS, [malformed], self.now)
        self.assertIsNone(q['liquidity_usd'])
        self.assertFalse(score_quote(q, {}, self.config, self.now)[3])

    def test_clock_rollback_pauses_io(self):
        radar = Radar(self.path, http=lambda *args: self.fail('No I/O allowed'), now=lambda: self.now)
        try:
            radar.store.set('last_tick', self.now + 100)
            radar.tick()
            self.assertTrue(radar.store.get('clock_warning'))
        finally: radar.close()

    def test_single_instance_lock(self):
        lock = Lock(self.path)
        try:
            with self.assertRaises(ValueError): Lock(self.path)
        finally: lock.close()
        Lock(self.path).close()

    def test_html_escapes_external_symbols(self):
        self.store.token(ADDRESS, '<img src=x onerror=alert(1)>', 'dex')
        render(self.path, self.store, self.config)
        output = (self.path / 'report.html').read_text()
        self.assertNotIn('<img src=x', output)
        self.assertIn('&lt;img', output)

    def test_brain_retry_after_crash_does_not_duplicate(self):
        radar = Radar(self.path, now=lambda: self.now)
        try:
            radar.store.remember('radar:stable:test', 'fact', 'Solana paper test, locally verified result', 'local ledger test', self.now)
            radar.sync_brain(self.config)
            with radar.store.db:
                radar.store.db.execute('UPDATE memories SET brain_id=NULL')
            radar.sync_brain(self.config)
            import brain
            store = brain.Store(self.path / 'memory.sqlite3')
            try: self.assertEqual(len(store.select('Solana')), 1)
            finally: store.close()
            self.assertIsNone(radar.store.get('brain_sync_error'))
        finally: radar.close()

    def test_canonical_move_keeps_existing_records_and_refuses_overwrite(self):
        import brain
        project = self.path / 'project'; project.mkdir()
        original = project / '.brain' / 'memory.sqlite3'
        store = brain.Store(original)
        store.add('preference', 'existing preference', 'user'); store.close()
        state = self.path / 'state'; state.mkdir()
        with patch('radar.service.ROOT', project): canonical_memory(state)
        self.assertTrue(original.is_symlink())
        store = brain.Store(original)
        try: self.assertEqual(len(store.select()), 1)
        finally: store.close()
        self.assertEqual(len(list(original.parent.glob('memory.before-radar-*.sqlite3'))), 1)


if __name__ == '__main__':
    unittest.main()
