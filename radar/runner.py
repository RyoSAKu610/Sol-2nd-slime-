"""Bounded polling and explicit, friction-aware paper simulation. No signer exists."""
from datetime import datetime, timezone
import hashlib
import json
import time

from . import config as settings
from .feeds import DEX, NANSEN, FeedError, nansen_specs, number, quote_for, request, rows
from .store import Store, dumps, iso


def score_quote(quote, sources, config, now):
    reasons, risks, score = [], ['売却可能性・保有集中・コントラクト権限は未検証',
        'DEX価格の更新時刻は提供されず、取得時刻のみ確認'], 0
    if not quote or now - quote['observed_at'] > config['quote_max_age_seconds'] or now < quote['observed_at']:
        return 0, reasons, risks + ['価格取得なし・古い・時計逆行'], False
    liquid = (quote['liquidity_usd'] or 0) >= config['min_liquidity_usd']
    active = (quote['volume_5m_usd'] or 0) >= config['min_volume_5m_usd']
    if liquid:
        score += 25; reasons.append('流動性が設定下限以上')
    else:
        risks.append('流動性が不足または不明')
    if active:
        score += 20; reasons.append('5分出来高が設定下限以上')
    else:
        risks.append('5分出来高が不足または不明')
    if (quote['buys_5m'] or 0) > (quote['sells_5m'] or 0):
        score += 10; reasons.append('5分買い件数が売り件数より多い')
    change = quote['change_5m_pct']
    if change is not None and 0 < change <= 25:
        score += 10; reasons.append('5分変化率が0〜25%')
    if change is not None and abs(change) > 25:
        risks.append('5分変化率が25%超、急変')
    sm = any(name.startswith('nansen:positive:') and 0 <= now - at <= 2 * config['smart_interval_seconds']
             for name, at in sources.items())
    if sm:
        score += 20; reasons.append('直近のNansen Smart Money純流入が正（因果・収益保証なし）')
    recent_activity = (quote['buys_5m'] or 0) + (quote['sells_5m'] or 0) > 0
    eligible = liquid and active and recent_activity and change is not None and abs(change) <= 25
    return score, reasons, risks, eligible


class Radar:
    def __init__(self, state, config_path=settings.DEFAULT_CONFIG, http=request, now=time.time):
        self.state, self.config_path, self.http, self.now = state, config_path, http, now
        self.store = Store(state / 'radar.sqlite3', now)
        self.last_key = None
        self.auth_blocked = False

    def close(self):
        self.store.close()

    def due(self, name):
        return self.store.feed(name)['next_at'] <= self.now()

    def failed(self, name, exc, state='error'):
        failures = self.store.feed(name)['failures']
        # Free DEX DNS failures can recover after wake while a long delay remains.
        # Keep other failure types and paid feeds on the original retry schedule.
        cap = 300 if name.startswith('dex:') and isinstance(exc, FeedError) and exc.category == 'dns' else 3600
        delay = min(cap, 60 * 2 ** min(failures, 6))
        if isinstance(exc, FeedError) and exc.retry_after:
            delay = max(delay, min(exc.retry_after, 86400))
        # Adapter messages are deliberately body-free. Unexpected exceptions only expose type.
        message = str(exc) if isinstance(exc, FeedError) else type(exc).__name__
        self.store.failure(name, message, delay, state)

    def poll_nansen(self, config):
        try:
            key = settings.read_key(config)
        except (OSError, ValueError):
            self.store.failure('nansen', 'Configured key file unavailable or not private', 60, 'credential_error')
            return
        if not key:
            old = self.store.feed('nansen')
            if old['state'] != 'missing_credential':
                self.store.failure('nansen', 'NANSEN_API_KEY / key_file not configured', 60, 'missing_credential')
            return
        if key != self.last_key:
            self.auth_blocked = False
            self.last_key = key
        if self.auth_blocked:
            return
        wallets = [row for row in self.store.rows('SELECT address FROM wallets ORDER BY last_checked,address')
                   if self.due('nansen:wallet:' + row['address'])][:1]
        specs = nansen_specs(config, self.now(), wallets[0]['address'] if wallets else None)
        for name, endpoint, payload, cost, interval in specs:
            name = 'nansen:' + name + (':' + wallets[0]['address'] if name == 'wallet' else '')
            if not self.due(name):
                continue
            try:
                credit = self.store.reserve(endpoint, cost, config)
            except ValueError:
                self.store.failure(name, 'Local credit ceiling or reported balance reached; no request', 3600, 'budget_paused')
                continue
            try:
                response, headers = self.http(NANSEN + endpoint, payload, key)
                lower = {k.lower(): v for k, v in headers.items()}
                self.store.charge(credit, 'ok', number(lower.get('x-nansen-credits-cost')))
                remaining = number(lower.get('x-nansen-credits-remaining'))
                if name == 'nansen:account':
                    if not isinstance(response, dict):
                        raise FeedError('Unexpected account schema')
                    account = response.get('data', response)
                    if not isinstance(account, dict):
                        raise FeedError('Unexpected account schema')
                    remaining = number(account.get('credits_remaining')) if remaining is None else remaining
                    self.store.set('account', {'plan': account.get('plan'), 'checked_at': self.now(),
                        'credits_remaining': remaining, 'reported_used': lower.get('x-nansen-credits-used')})
                    records = []
                else:
                    records = rows(response)
                    self.ingest_nansen(name, records)
                    self.store.set('coverage:' + name, {'first_page_only': True,
                        'pagination': response.get('pagination'), 'checked_at': self.now()})
                if remaining is not None:
                    self.store.set('balance', {'remaining_estimate': remaining, 'checked_at': self.now()})
                self.store.success(name, records, len(records), interval)
                self.store.success('nansen', [], len(records), interval)
                if name.startswith('nansen:wallet:'):
                    with self.store.db:
                        self.store.db.execute('UPDATE wallets SET last_checked=? WHERE address=?', (self.now(), wallets[0]['address']))
            except (FeedError, ValueError, TypeError) as exc:
                self.store.charge(credit, 'failed_conservative_reservation')
                self.failed(name, exc)
                if isinstance(exc, FeedError) and exc.status in (401, 402, 403):
                    self.auth_blocked = True
                    self.store.failure('nansen', f'HTTP {exc.status}; blocked until key change / restart', 86400, 'auth_or_plan_blocked')
                    break

    def ingest_nansen(self, name, records):
        for record in records:
            if record.get('chain', 'solana') != 'solana':
                continue
            addresses = [(record.get('token_address'), record.get('token_symbol'))]
            if name == 'nansen:dex-trades':
                # Preserve both swap legs. Merely appearing here gives no directional score.
                addresses.extend((record.get('token_' + side + '_address'), record.get('token_' + side + '_symbol'))
                                 for side in ('bought', 'sold'))
            for field in ('tokens_sent', 'tokens_received'):
                for token in record.get(field) or []:
                    if isinstance(token, dict):
                        addresses.append((token.get('token_address'), token.get('token_symbol')))
            for address, symbol in addresses:
                if isinstance(address, str) and settings.ADDRESS.fullmatch(address):
                    self.store.token(address, symbol, name)
                    positive = ((name == 'nansen:tokens' and (number(record.get('netflow')) or 0) > 0)
                                or (name == 'nansen:netflow' and (number(record.get('net_flow_1h_usd')) or 0) > 0))
                    if positive:
                        self.store.token(address, symbol, 'nansen:positive:' + name.rsplit(':', 1)[-1])
                    elif name in ('nansen:tokens', 'nansen:netflow'):
                        source_row = self.store.rows('SELECT sources FROM tokens WHERE address=?', (address,))[0]
                        sources = json.loads(source_row['sources'])
                        sources.pop('nansen:positive:' + name.rsplit(':', 1)[-1], None)
                        with self.store.db:
                            self.store.db.execute('UPDATE tokens SET sources=? WHERE address=?', (dumps(sources), address))
            # The API label means historically classified Smart Money, never a promise of skill.
            for field in ('trader_address', 'wallet_address'):
                address = record.get(field)
                if name == 'nansen:dex-trades' and isinstance(address, str) and settings.ADDRESS.fullmatch(address):
                    count = self.store.db.execute('SELECT COUNT(*) FROM wallets').fetchone()[0]
                    if count < 5:
                        self.store.watch(address, 'Nansen Smart Money discovery; future profitability unverified')

    def poll_dex(self, config):
        if self.due('dex:profiles'):
            try:
                payload, _ = self.http(DEX + '/token-profiles/latest/v1')
                if not isinstance(payload, list):
                    raise FeedError('Unexpected profiles schema')
                records = [r for r in payload if isinstance(r, dict) and r.get('chainId') == 'solana'
                           and isinstance(r.get('tokenAddress'), str) and settings.ADDRESS.fullmatch(r['tokenAddress'])]
                for row in records:
                    self.store.token(row['tokenAddress'], '', 'dex:profile')
                self.store.success('dex:profiles', records, len(records), config['discovery_interval_seconds'])
            except (FeedError, ValueError, TypeError) as exc:
                self.failed('dex:profiles', exc)
        candidates = self.store.rows('''SELECT address FROM tokens ORDER BY
            (address IN (SELECT address FROM trades WHERE closed_at IS NULL)) DESC,
            last_seen DESC,score DESC LIMIT ?''', (config['max_candidates'],))
        for offset in range(0, len(candidates), 30):
            addresses = [row['address'] for row in candidates[offset:offset + 30]]
            name = 'dex:quotes:' + str(offset // 30)
            if not self.due(name):
                continue
            try:
                payload, _ = self.http(DEX + '/tokens/v1/solana/' + ','.join(addresses))
                if not isinstance(payload, list):
                    raise FeedError('Unexpected quotes schema')
                self.store.success(name, payload, len(payload), config['loop_seconds'])
                for address in addresses:
                    quote = quote_for(address, payload, self.now())
                    if quote:
                        with self.store.db:
                            self.store.db.execute('UPDATE tokens SET quote=?,quote_at=?,symbol=? WHERE address=?',
                                (dumps(quote), self.now(), quote['symbol'], address))
            except (FeedError, ValueError, TypeError) as exc:
                self.failed(name, exc)

    def simulate(self, config):
        now = self.now()
        self.store.paper(config)  # Initialize once before the atomic trading transaction.
        tokens = self.store.rows('SELECT * FROM tokens WHERE quote_at>=?', (now - config['quote_max_age_seconds'],))
        scored = []
        for token in tokens:
            q = json.loads(token['quote'])
            score, reasons, risks, eligible = score_quote(q, json.loads(token['sources']), config, now)
            with self.store.db:
                self.store.db.execute('UPDATE tokens SET score=?,rationale=?,risks=? WHERE address=?',
                    (score, dumps(reasons), dumps(risks), token['address']))
            scored.append((score, token, q, eligible))
        quotes = {t['address']: q for _, t, q, _ in scored}
        # All order events and capital checks share the SQLite write transaction.
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            paper = self.store.paper(config)
            for trade in paper['open']:
                q = quotes.get(trade['address'])
                if not q or (q['buys_5m'] or 0) + (q['sells_5m'] or 0) <= 0:
                    continue
                price = q['price_usd'] * (1 - config['slippage_fraction'])
                proceeds = max(0, trade['quantity'] * price * (1 - config['fee_fraction']) - config['network_fee_usd'])
                change = proceeds / trade['outlay'] - 1
                reason = 'take_profit' if change >= config['take_profit_fraction'] else 'stop_loss' if change <= -config['stop_loss_fraction'] else 'timeout' if now - trade['opened_at'] >= config['max_hold_seconds'] else None
                if reason:
                    self.store.db.execute('UPDATE trades SET closed_at=?,exit_price=?,proceeds=?,pnl=?,reason=? WHERE id=? AND closed_at IS NULL',
                        (now, price, proceeds, proceeds - trade['outlay'], reason, trade['id']))
            paper = self.store.paper(config)
            for score, token, q, eligible in sorted(scored, key=lambda item: -item[0]):
                if not eligible or score < config['entry_score']:
                    continue
                address = token['address']
                if len(paper['open']) >= config['max_positions'] or paper['cash'] - config['position_usd'] < config['reserve_usd']:
                    break
                last = self.store.rows('SELECT * FROM trades WHERE address=? ORDER BY id DESC LIMIT 1', (address,))
                if last and (last[0]['closed_at'] is None or now - last[0]['closed_at'] < config['cooldown_seconds']):
                    continue
                outlay = config['position_usd']
                spendable = (outlay - config['network_fee_usd']) / (1 + config['fee_fraction'])
                if spendable <= 0:
                    continue
                price = q['price_usd'] * (1 + config['slippage_fraction'])
                quantity = spendable / price
                event = f"paper:solana:{address}:{now:.6f}"
                self.store.db.execute('INSERT INTO trades(event_key,address,symbol,opened_at,entry_price,quantity,outlay,entry_cost) VALUES (?,?,?,?,?,?,?,?)',
                    (event, address, token['symbol'], now, price, quantity, outlay, outlay - quantity * q['price_usd']))
                paper = self.store.paper(config)
        for trade in self.store.rows('SELECT * FROM trades WHERE closed_at IS NOT NULL'):
            self.store.remember('radar:' + trade['event_key'], 'fact',
                f"PAPER Solana {trade['symbol']} {trade['address']}：{iso(trade['opened_at'])}〜{iso(trade['closed_at'])}、投入${trade['outlay']:.4f}、手数料・仮定slippage控除後損益${trade['pnl']:.6f}、終了理由{trade['reason']}。実売買・収益優位の証明ではない。",
                f"radar ledger trade {trade['id']}; {DEX}/tokens/v1/solana/{trade['address']}", trade['closed_at'])
        # At most five new hypotheses per UTC day; repeated ticks keep immutable first evidence.
        day = iso(now)[:10]
        created = self.store.db.execute('SELECT COUNT(*) FROM memories WHERE event_key LIKE ?', ('radar:candidate:' + day + ':%',)).fetchone()[0]
        for score, token, q, eligible in sorted(scored, key=lambda item: -item[0]):
            if created >= 5:
                break
            key = 'radar:candidate:' + day + ':' + token['address']
            if eligible and score >= config['entry_score'] and not self.store.rows('SELECT event_key FROM memories WHERE event_key=?', (key,)):
                self.store.remember(key, 'hypothesis', f"Solana候補 {token['symbol']} {token['address']}：{iso(now)}時点のルール得点{score}、取得価格${q['price_usd']}、流動性${q['liquidity_usd']}。将来利益・売却可能性は未検証。",
                    q['source'] + '; observed_at=' + iso(now), now)
                created += 1

    def sync_brain(self, config):
        if not config['brain_sync']:
            return
        import brain
        store = brain.Store(self.state / 'memory.sqlite3')
        try:
            for event in self.store.rows('SELECT * FROM memories WHERE brain_id IS NULL ORDER BY at LIMIT 20'):
                entry = store.add(event['kind'], event['content'], event['source'],
                    dedupe_key=event['event_key'], verified=event['kind'] == 'fact',
                    review_at=iso(event['at'] + 7 * 86400),
                    expires_at=iso(event['at'] + 86400) if event['kind'] == 'hypothesis' else None)
                with self.store.db:
                    self.store.db.execute('UPDATE memories SET brain_id=? WHERE event_key=?', (entry['id'], event['event_key']))
            # Reference prior relevant evidence, but never execute instructions stored in memory.
            references = store.select('Solana', limit=8)
            self.store.set('brain_references', [{key: entry.get(key) for key in ('id', 'content', 'classification', 'source')} for entry in references])
            self.store.set('brain_sync_error', None)
        except (ValueError, TypeError) as exc:
            self.store.set('brain_sync_error', type(exc).__name__)
        finally:
            store.close()

    def tick(self):
        config = settings.load(self.config_path)
        now = self.now()
        previous = self.store.get('last_tick')
        if previous is not None and now < previous:
            self.store.set('clock_warning', 'Clock moved backwards; polling and paper actions paused')
            return config
        self.store.set('clock_warning', None)
        self.store.set('last_tick', now)
        for address in config['wallets']:
            self.store.watch(address)
        self.poll_nansen(config)
        self.poll_dex(config)
        self.simulate(config)
        self.sync_brain(config)
        self.store.cleanup(config['max_candidates'])
        self.store.set('last_completed_tick', self.now())
        return config
