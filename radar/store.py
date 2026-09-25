"""Durable observations, conservative credit reservation, and paper ledger."""
from datetime import datetime, timezone
import json
import hashlib
import sqlite3
import time


def iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat(timespec='seconds')


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, path, now=time.time):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.now = now
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feeds(name TEXT PRIMARY KEY, last_attempt REAL,
          last_success REAL, last_error TEXT, failures INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0, count INTEGER, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, feed TEXT,
          at REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS credits(id INTEGER PRIMARY KEY, at REAL NOT NULL,
          endpoint TEXT NOT NULL, reserved REAL NOT NULL, charged REAL NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tokens(address TEXT PRIMARY KEY, symbol TEXT NOT NULL DEFAULT '',
          first_seen REAL NOT NULL, last_seen REAL NOT NULL, sources TEXT NOT NULL,
          quote TEXT, quote_at REAL, score INTEGER NOT NULL DEFAULT 0,
          rationale TEXT NOT NULL DEFAULT '[]', risks TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS wallets(address TEXT PRIMARY KEY, added_at REAL,
          last_checked REAL NOT NULL DEFAULT 0, label TEXT NOT NULL DEFAULT 'user watchlist');
        CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
          address TEXT NOT NULL, symbol TEXT, opened_at REAL NOT NULL,
          entry_price REAL NOT NULL, quantity REAL NOT NULL, outlay REAL NOT NULL,
          entry_cost REAL NOT NULL, closed_at REAL, exit_price REAL, proceeds REAL,
          pnl REAL, reason TEXT, brain_id INTEGER);
        CREATE INDEX IF NOT EXISTS credit_time ON credits(at);
        CREATE INDEX IF NOT EXISTS observation_time ON observations(at);
        CREATE TABLE IF NOT EXISTS memories(event_key TEXT PRIMARY KEY,kind TEXT NOT NULL,
          content TEXT NOT NULL,source TEXT NOT NULL,at REAL NOT NULL,brain_id INTEGER);
        ''')
        # Additive migration: preserve every old observation; future repeats aggregate.
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(observations)')}
        for name, declaration in [('fingerprint', 'TEXT'), ('first_at', 'REAL'), ('seen_count', 'INTEGER NOT NULL DEFAULT 1')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE observations ADD COLUMN {name} {declaration}')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS observation_identity ON observations(feed,fingerprint)')
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, dumps(value)))

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def feed(self, name):
        rows = self.rows('SELECT * FROM feeds WHERE name=?', (name,))
        return rows[0] if rows else {'name': name, 'next_at': 0, 'failures': 0, 'state': 'not_attempted'}

    def success(self, name, payload, count, interval):
        now = self.now()
        with self.db:
            self.db.execute('''INSERT INTO feeds(name,last_attempt,last_success,last_error,failures,next_at,count,state)
                VALUES (?,?,?,NULL,0,?,?,?) ON CONFLICT(name) DO UPDATE SET
                last_attempt=excluded.last_attempt,last_success=excluded.last_success,last_error=NULL,
                failures=0,next_at=excluded.next_at,count=excluded.count,state=excluded.state''',
                (name, now, now, now + interval, count, 'ok' if count else 'ok_empty'))
            values = payload if isinstance(payload, list) else [payload]
            for value in values:
                raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                digest = hashlib.sha256(raw.encode()).hexdigest()
                if isinstance(value, dict) and value.get('transaction_hash'):
                    identity = [value.get(k) for k in ('chain', 'transaction_hash', 'log_index', 'token_bought_address', 'token_sold_address')]
                    digest = 'tx:' + hashlib.sha256(dumps(identity).encode()).hexdigest()
                self.db.execute('''INSERT INTO observations(feed,at,payload,fingerprint,first_at) VALUES (?,?,?,?,?)
                    ON CONFLICT(feed,fingerprint) DO UPDATE SET at=excluded.at,payload=excluded.payload,seen_count=observations.seen_count+1''',
                    (name, now, raw, digest, now))

    def failure(self, name, error, delay, state='error'):
        now = self.now()
        with self.db:
            self.db.execute('''INSERT INTO feeds(name,last_attempt,last_error,failures,next_at,state)
                VALUES (?,?,?,1,?,?) ON CONFLICT(name) DO UPDATE SET last_attempt=excluded.last_attempt,
                last_error=excluded.last_error,failures=feeds.failures+1,next_at=excluded.next_at,state=excluded.state''',
                (name, now, error, now + delay, state))

    def credit_totals(self):
        now = datetime.fromtimestamp(self.now(), timezone.utc)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        return {'day': self.db.execute('SELECT COALESCE(SUM(charged),0) FROM credits WHERE at>=?', (day,)).fetchone()[0],
                'month': self.db.execute('SELECT COALESCE(SUM(charged),0) FROM credits WHERE at>=?', (month,)).fetchone()[0]}

    def reserve(self, endpoint, cost, config):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            totals = self.credit_totals()
            balance = self.get('balance', {})
            if totals['day'] + cost > config['daily_credits'] or totals['month'] + cost > config['monthly_credits']:
                raise ValueError('Local API credit ceiling reached; no HTTP request made')
            if balance and balance.get('remaining_estimate', float('inf')) < cost:
                raise ValueError('Reported credit balance exhausted; no HTTP request made')
            row = self.db.execute('INSERT INTO credits(at,endpoint,reserved,charged,status) VALUES (?,?,?,?,?)',
                                  (self.now(), endpoint, cost, cost, 'reserved'))
            if balance and 'remaining_estimate' in balance:
                balance['remaining_estimate'] = max(0, balance['remaining_estimate'] - cost)
                self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', ('balance', dumps(balance)))
            return row.lastrowid

    def charge(self, credit_id, status, actual=None):
        # Failed/ambiguous calls keep the full reservation. This is an upper bound, not an invoice.
        with self.db:
            self.db.execute('UPDATE credits SET status=? WHERE id=?', (status, credit_id))
            if actual is not None:
                self.db.execute('UPDATE credits SET charged=MAX(reserved,?) WHERE id=?', (actual, credit_id))

    def token(self, address, symbol, source):
        row = self.db.execute('SELECT sources FROM tokens WHERE address=?', (address,)).fetchone()
        sources = json.loads(row[0]) if row else {}
        sources[source] = self.now()
        with self.db:
            self.db.execute('''INSERT INTO tokens(address,symbol,first_seen,last_seen,sources) VALUES (?,?,?,?,?)
                ON CONFLICT(address) DO UPDATE SET symbol=CASE WHEN excluded.symbol!='' THEN excluded.symbol ELSE tokens.symbol END,
                last_seen=excluded.last_seen,sources=excluded.sources''', (address, str(symbol or '')[:100], self.now(), self.now(), dumps(sources)))

    def watch(self, address, label='user watchlist'):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO wallets(address,added_at,label) VALUES (?,?,?)', (address, self.now(), label))

    def cleanup(self, max_tokens):
        # max_tokens bounds the ACTIVE quote set, never erases long-term evidence.
        self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')

    def prune_observations(self, max_rows=5000):
        """GHA artifact pruning helper only. Not used by tick(); resident history is preserved."""
        if not isinstance(max_rows, int) or max_rows < 0:
            raise ValueError('max_rows must be a nonnegative integer')
        with self.db:
            total = self.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
            if total <= max_rows:
                return 0
            if max_rows == 0:
                self.db.execute('DELETE FROM observations')
                return total
            self.db.execute('DELETE FROM observations WHERE id NOT IN'
                            ' (SELECT id FROM observations ORDER BY at DESC, id DESC LIMIT ?)', (max_rows,))
            return total - max_rows

    def remember(self, key, kind, content, source, at):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO memories(event_key,kind,content,source,at) VALUES (?,?,?,?,?)',
                            (key, kind, content, source, at))

    def history(self, query='', limit=20):
        # Literal search: %/_ supplied by users are not SQL wildcard operators.
        query = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        return self.rows("SELECT id,feed,first_at,at,seen_count,payload FROM observations WHERE payload LIKE ? ESCAPE '\\' OR feed LIKE ? ESCAPE '\\' ORDER BY at DESC LIMIT ?",
                         ('%' + query + '%', '%' + query + '%', limit))

    def paper(self, config):
        # The initial cash is fixed on first use; later config edits cannot mint paper cash.
        if self.get('initial_cash') is None:
            self.set('initial_cash', config['initial_cash_usd'])
        initial = self.get('initial_cash')
        rows = self.rows('SELECT * FROM trades ORDER BY id')
        cash = initial - sum(t['outlay'] for t in rows) + sum(t['proceeds'] or 0 for t in rows)
        return {'mode': 'paper', 'initial_cash': initial, 'cash': round(cash, 10),
                'realized_pnl': sum(t['pnl'] or 0 for t in rows),
                'open': [t for t in rows if t['closed_at'] is None], 'closed': [t for t in rows if t['closed_at'] is not None]}
