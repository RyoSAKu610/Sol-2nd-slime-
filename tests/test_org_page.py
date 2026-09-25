"""Org activity page tests: fixture generation, 15 roles, no secrets, <1MB."""
import json
from pathlib import Path
import tempfile
import unittest

from radar import gha_snapshot, org_page
from radar.config import load
from radar.store import Store, dumps

ROOT = Path(__file__).resolve().parent.parent
GHA_CONFIG = ROOT / "radar" / "config.gha.json"
ADDRESS = "So11111111111111111111111111111111111111112"
OTHER = "11111111111111111111111111111111"
SECRET = "sk-test-ORG-SECRET-XYZ-123456789"
WALLET = "4uQeVj5tqViQh7y8o7p9q3r5s6t7u8v9w0x1y2z3a4b5"
NAMES = ["ピカード", "ヘイムダル", "ファインマン", "デミング", "ポパー",
         "シカマル", "リスコフ", "ハミルトン", "コロンボ", "タンタン",
         "エーコ", "スポック", "アルゴス", "コンウェイ", "カーネマン"]


def quote(now, price=1.5):
    return {"chain": "solana", "address": ADDRESS, "pair_address": OTHER,
            "symbol": "TEST", "price_usd": price, "liquidity_usd": 100000,
            "volume_5m_usd": 10000, "buys_5m": 40, "sells_5m": 10,
            "change_5m_pct": 10, "pair_created_at": 1600000000,
            "observed_at": now, "source_price_updated_at": None,
            "source": "https://api.dexscreener.com/tokens/v1/solana/" + ADDRESS}


class OrgPageTests(unittest.TestCase):
    def test_fixture_generates_15_roles_without_secrets_under_1mb(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        now = 1789000000.0
        config = load(GHA_CONFIG)
        store = Store(path / "radar.sqlite3", lambda: now)
        try:
            store.token(ADDRESS, "TEST", "dex:profile")
            with store.db:
                store.db.execute("UPDATE tokens SET quote=?,quote_at=?,score=?,rationale=?,risks=?",
                                 (dumps(quote(now)), now, 65, dumps(["流動性が設定下限以上"]), dumps(["未検証"])))
            store.success("dex:profiles", [{"chainId": "solana"}], 1, 60)
            # Secret-bearing payloads and a wallet must never reach the page.
            store.success("dex:quotes:0", [{"leak": SECRET}], 1, 60)
            store.failure("nansen", "custom failure with " + SECRET, 60)
            store.watch(WALLET)
            store.remember("radar:candidate:2026-01-01:" + ADDRESS, "hypothesis",
                           "candidate with " + SECRET, "test-source", now)
            store.set("initial_cash", 20)
        finally:
            store.close()
        slim = gha_snapshot.build_slim(path / "radar.sqlite3", config)
        doc = org_page.build_org_html(slim)
        out = path / "org.html"
        out.write_text(doc)
        # 15 roles displayed with 2-value states only.
        for name in NAMES:
            self.assertIn(name, doc)
        self.assertIn("待機", doc)
        self.assertIn("稼働中", doc)
        self.assertNotIn(SECRET, doc)
        self.assertNotIn("SECRET-XYZ", doc)
        self.assertNotIn(WALLET, doc)
        self.assertNotIn('"payload"', doc)
        self.assertNotIn('"leak"', doc)
        self.assertNotIn("http://", doc)
        self.assertNotIn("<script", doc)
        self.assertLess(out.stat().st_size, 1_048_576)
        # Hypothesis board shows ID/mint/score/expiry only, no profit promise.
        self.assertIn(ADDRESS, doc)
        self.assertNotIn("将来利益は保証", doc.split("仮説ボード")[1].split("紙成績")[0])
        # CLI path also succeeds on the same fixture.
        gha_snapshot.write_slim(slim, path / "slim.json")
        self.assertEqual(org_page.main(["--slim", str(path / "slim.json"),
                                        "--out", str(path / "cli-org.html")]), 0)
        self.assertLess((path / "cli-org.html").stat().st_size, 1_048_576)


if __name__ == "__main__":
    unittest.main()
