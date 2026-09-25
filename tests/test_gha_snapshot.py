"""GHA slim snapshot tests: size, secret exclusion, and missing-data behavior."""
import json
from pathlib import Path
import tempfile
import unittest

from radar import gha_snapshot
from radar.config import load
from radar.store import Store, dumps

ROOT = Path(__file__).resolve().parent.parent
GHA_CONFIG = ROOT / "radar" / "config.gha.json"
ADDRESS = "So11111111111111111111111111111111111111112"
OTHER = "11111111111111111111111111111111"


def quote(now, price=1.5):
    return {"chain": "solana", "address": ADDRESS, "pair_address": OTHER,
            "symbol": "TEST", "price_usd": price, "liquidity_usd": 100000,
            "volume_5m_usd": 10000, "buys_5m": 40, "sells_5m": 10,
            "change_5m_pct": 10, "pair_created_at": 1600000000,
            "observed_at": now, "source_price_updated_at": None,
            "source": "https://api.dexscreener.com/tokens/v1/solana/" + ADDRESS}


class GhaSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.now = 1789000000.0
        self.config = load(GHA_CONFIG)

    def _store(self):
        return Store(self.path / "radar.sqlite3", lambda: self.now)

    def test_slim_under_1mb_with_expected_sections(self):
        store = self._store()
        try:
            store.token(ADDRESS, "TEST", "dex:profile")
            with store.db:
                store.db.execute("UPDATE tokens SET quote=?,quote_at=?,score=?,rationale=?,risks=?",
                                 (dumps(quote(self.now)), self.now, 65, dumps(["流動性が設定下限以上"]), dumps(["未検証"])))
            with store.db:
                store.db.execute("INSERT INTO trades(event_key,address,symbol,opened_at,entry_price,quantity,outlay,entry_cost)"
                                 " VALUES (?,?,?,?,?,?,?,?)",
                                 ("paper:solana:test:1", ADDRESS, "TEST", self.now, 1.5, 1.0, 2.0, 0.5))
            store.success("dex:profiles", [{"chainId": "solana"}], 1, 60)
            store.set("initial_cash", 20)
        finally:
            store.close()
        out = self.path / "slim.json"
        slim = gha_snapshot.build_slim(self.path / "radar.sqlite3", self.config)
        gha_snapshot.write_slim(slim, out)
        size = out.stat().st_size
        self.assertLess(size, 1_048_576)
        body = json.loads(out.read_text())
        for key in ("tokens", "paper", "feeds", "credits", "notes", "config_digest", "counts"):
            self.assertIn(key, body)
        self.assertEqual(body["series"], "gha-slim")
        self.assertEqual(body["paper"]["mode"], "paper")
        self.assertIn("将来利益", body["notes"]["no_guarantee"])
        self.assertIn("別系列", body["notes"]["compatibility"])
        self.assertEqual(body["tokens"][0]["price_usd"], 1.5)
        # Payload全文を含めない: observationsテーブル自体は出さず件数のみ、token/paper/feedにpayloadキーなし
        self.assertNotIn("observations", body)
        dumped = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("sk-test", dumped)
        for token in body["tokens"]:
            self.assertNotIn("payload", token)

    def test_secrets_and_payload_never_included(self):
        secret = "sk-test-SECRET-XYZ-987654321"
        store = self._store()
        try:
            store.success("dex:profiles", [{"leak": secret, "p": 1}], 1, 60)
            store.failure("dex:quotes:0", "custom failure with " + secret, 60)
            store.set("balance", {"remaining_estimate": 5, "checked_at": self.now})
        finally:
            store.close()
        slim = gha_snapshot.build_slim(self.path / "radar.sqlite3", self.config)
        out = self.path / "slim.json"
        gha_snapshot.write_slim(slim, out)
        raw = out.read_text()
        self.assertNotIn(secret, raw)
        self.assertNotIn("SECRET-XYZ", raw)
        # WAL補助ファイルを読まない: 出力にwal/shmの語を含めず、副ファイルを作らない
        self.assertNotIn("-wal", raw)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse((self.path / ("radar.sqlite3" + suffix)).exists()
                             and ("leak" in (self.path / ("radar.sqlite3" + suffix)).read_bytes().decode("utf-8", "ignore")),
                             suffix)

    def test_missing_quote_and_empty_db(self):
        store = self._store()
        try:
            store.token(OTHER, "NOQUOTE", "dex:profile")
        finally:
            store.close()
        slim = gha_snapshot.build_slim(self.path / "radar.sqlite3", self.config)
        by_address = {t["address"]: t for t in slim["tokens"]}
        self.assertIn(OTHER, by_address)
        self.assertIsNone(by_address[OTHER]["price_usd"])
        self.assertFalse(by_address[OTHER]["fresh"])
        self.assertEqual(slim["paper"]["open_count"], 0)
        self.assertEqual(slim["paper"]["closed_count"], 0)
        # feeds/creditsが空でも落ちない
        self.assertIsInstance(slim["feeds"], list)
        self.assertIn("day", slim["credits"])
        # CLI経由でも欠測DBで成功する
        out = self.path / "cli-slim.json"
        code = gha_snapshot.main(["--db", str(self.path / "radar.sqlite3"),
                                  "--out", str(out), "--config", str(GHA_CONFIG)])
        self.assertEqual(code, 0)
        self.assertLess(out.stat().st_size, 1_048_576)


if __name__ == "__main__":
    unittest.main()
