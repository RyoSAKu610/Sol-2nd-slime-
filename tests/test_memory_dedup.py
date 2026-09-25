from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import brain


V1_SCHEMA = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
    created_at TEXT NOT NULL, verified_at TEXT, verified_source TEXT, reviewed_at TEXT,
    review_at TEXT, expires_at TEXT, status TEXT NOT NULL DEFAULT 'active',
    supersedes INTEGER UNIQUE REFERENCES memories(id), seed_key TEXT UNIQUE
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL REFERENCES memories(id),
    action TEXT NOT NULL, at TEXT NOT NULL, details TEXT NOT NULL
);
PRAGMA user_version=1;
"""


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.now = datetime(2026, 9, 12, 0, 0, tzinfo=timezone.utc)
        self.store = brain.Store(self.path, now=lambda: self.now)
        self.addCleanup(self.store.close)

    def test_normalized_repeat_and_sources_survive_restart(self):
        first = self.store.add("fact", "残高　２０ USD", "出典　Ａ")
        self.now += timedelta(hours=1)
        repeat = self.store.add("fact", "  残高\n20   USD  ", "出典 A")
        other_source = self.store.add("fact", "残高 20 USD", "別の照合元")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(repeat["deduplicated"])
        self.assertEqual(first["id"], repeat["id"])
        self.assertEqual(len(other_source["sources"]), 2)
        self.assertEqual(other_source["ingestion_count"], 3)
        self.assertLess(other_source["first_seen"], other_source["last_seen"])
        with closing(brain.Store(self.path)) as reopened:
            self.assertEqual(reopened.select("別の照合元")[0]["id"], first["id"])
            self.assertEqual(len(reopened.select()), 1)
            self.assertIn("別の照合元", brain.render_context(reopened, "残高"))
            self.assertEqual(len(reopened.show(first["id"])["selected"]["sources"]), 2)

    def test_case_sensitive_addresses_numbers_negation_and_kind_do_not_merge(self):
        entries = [
            ("fact", "wallet AbCdEF 20 USD"),
            ("fact", "wallet abcdef 20 USD"),
            ("fact", "wallet AbCdEF 200 USD"),
            ("fact", "wallet AbCdEF 20 USDではない"),
            ("hypothesis", "wallet AbCdEF 20 USD"),
            ("fact", "wallet AbCdEG 20 USD"),
        ]
        ids = {self.store.add(kind, text, "根拠")["id"] for kind, text in entries}
        self.assertEqual(len(ids), len(entries))

    def test_distinct_event_keys_keep_identical_transactions_separate(self):
        one = self.store.add("fact", "buy 1 SOL", "tx", dedupe_key="solana:TxA:0")
        two = self.store.add("fact", "buy 1 SOL", "tx", dedupe_key="solana:Txa:0")
        three = self.store.add("fact", "buy 1 SOL", "tx", dedupe_key="solana:TxA:1")
        four = self.store.add("fact", "buy 1 SOL", "summary")
        self.assertEqual(len({one["id"], two["id"], three["id"], four["id"]}), 4)
        repeat = self.store.add("fact", "buy 1 SOL", "tx", dedupe_key="solana:TxA:0")
        self.assertEqual(one["id"], repeat["id"])
        self.assertTrue(repeat["deduplicated"])

    def test_same_key_content_conflict_is_explicit_and_rolls_back(self):
        saved = self.store.add("fact", "buy 1 SOL", "receipt", dedupe_key="event")
        before = self.store.stats()
        with self.assertRaises(brain.DuplicateConflict):
            self.store.add("fact", "buy 2 SOL", "changed receipt", dedupe_key="event")
        with self.assertRaises(brain.DuplicateConflict):
            self.store.add("hypothesis", "buy 1 SOL", "changed classification", dedupe_key="event")
        self.assertEqual(self.store.stats(), before)
        self.assertEqual(self.store.get(saved["id"])["content"], "buy 1 SOL")

    def test_repeat_does_not_promote_or_extend_dates(self):
        expiry = brain.stamp(self.now + timedelta(days=1))
        saved = self.store.add("fact", "過去の見積り", "見積り元", review_at=expiry, expires_at=expiry)
        self.now += timedelta(days=3)
        repeat = self.store.add("fact", "過去の見積り", "再取り込み", verified=True,
                                review_at=brain.stamp(self.now + timedelta(days=10)),
                                expires_at=brain.stamp(self.now + timedelta(days=10)))
        self.assertEqual(repeat["id"], saved["id"])
        for field in ("verified_at", "verified_source", "reviewed_at", "review_at", "expires_at"):
            self.assertEqual(repeat[field], saved[field], field)
        self.assertTrue(self.store.annotate(repeat)["expired"])
        self.assertNotIn("過去の見積り", brain.render_context(self.store, "見積り"))
        for _ in range(3):
            self.store.get(saved["id"])
            self.store.show(saved["id"])
            self.store.select("見積り")
        self.assertEqual(self.store.get(saved["id"])["ingestion_count"], 2)

    def test_repeated_verified_memory_keeps_original_verification_time(self):
        saved = self.store.add("fact", "確認した事実", "確認元", verified=True)
        self.now += timedelta(days=2)
        repeat = self.store.add("fact", "確認した事実", "別情報元", verified=True)
        self.assertEqual(saved["verified_at"], repeat["verified_at"])
        self.assertEqual(saved["verified_source"], repeat["verified_source"])

    def test_old_content_reingestion_does_not_resurrect_either_identity_mode(self):
        for key in (None, "event"):
            with self.subTest(key=key):
                old = self.store.add("fact", "予定 金曜日", "発表", dedupe_key=key, verified=True)
                new = self.store.supersede(old["id"], "予定 月曜日", "訂正", verified=True)
                repeat = self.store.add("fact", "予定 金曜日", "古い再送", dedupe_key=key, verified=True)
                self.assertEqual(repeat["id"], old["id"])
                self.assertEqual(repeat["current_id"], new["id"])
                self.assertEqual(repeat["status"], "superseded")
                self.assertNotIn("金曜日", brain.render_context(self.store, "予定"))
                self.assertNotIn("古い再送", json.dumps(new["sources"], ensure_ascii=False))
                latest = self.store.add("fact", "予定 月曜日", "訂正", dedupe_key=key)
                self.assertEqual(latest["id"], new["id"])

    def test_explicit_reversion_creates_a_version_without_duplicate_current_content(self):
        a = self.store.add("decision", "方針 A", "1")
        b = self.store.supersede(a["id"], "方針 B", "2")
        c = self.store.supersede(b["id"], "方針 A", "3")
        self.assertEqual(self.store.add("decision", "方針 A", "4")["id"], c["id"])
        self.assertEqual(len(self.store.select()), 1)
        self.assertEqual(len(self.store.show(a["id"])["versions"]), 3)

    def test_bootstrap_reuses_manual_seed_and_preserves_unverified_state(self):
        with closing(brain.Store(Path(self.temp.name) / "reference.sqlite3")) as reference:
            reference.bootstrap()
            content = reference.get(1)["content"]
        manual = self.store.add("preference", content, "ユーザーが手動登録")
        result = self.store.bootstrap()
        self.assertEqual(result["reused_ids"], [manual["id"]])
        self.assertEqual(len(result["created_ids"]), 2)
        self.assertIsNone(self.store.get(manual["id"])["verified_at"])
        self.store.supersede(manual["id"], "変更した回答形式", "明示訂正")
        self.assertEqual(self.store.bootstrap()["created_ids"], [])
        self.assertEqual(len(self.store.select()), 3)

    def test_manual_merge_preserves_original_sources_and_verification(self):
        a = self.store.add("preference", "根拠を示す回答を希望", "発言1", verified=True)
        b = self.store.add("preference", "回答には出典を付ける", "発言2")
        # Paraphrases survive until the user/agent explicitly verifies equivalence.
        self.assertEqual(len(self.store.select()), 2)
        merged = self.store.merge(a["id"], b["id"], "両方とも出典付与の同じ希望と確認")
        self.assertIsNone(merged["verified_at"])
        self.assertEqual(len(merged["sources"]), 2)
        self.assertEqual(len(self.store.select()), 1)
        self.assertEqual(len(self.store.select(include_history=True)), 2)
        self.assertEqual(self.store.show(a["id"])["current"]["id"], b["id"])
        repeat = self.store.add("preference", a["content"], "発言1")
        self.assertEqual(repeat["id"], b["id"])
        corrected = self.store.supersede(b["id"], "重要事項に出典を付ける", "訂正")
        self.assertEqual(self.store.show(a["id"])["current"]["id"], corrected["id"])
        repeat_old = self.store.add("preference", a["content"], "発言1")
        self.assertEqual(repeat_old["status"], "superseded")
        self.assertEqual(repeat_old["current_id"], corrected["id"])
        self.assertTrue(self.store.audit()["ok"])

    def test_invalid_merge_and_unchanged_correction_fail_without_writes(self):
        a = self.store.add("fact", "同じ事実", "出典")
        b = self.store.add("fact", "別イベント", "出典", dedupe_key="tx")
        before = self.store.stats()
        for call in (
            lambda: self.store.merge(a["id"], a["id"], "根拠"),
            lambda: self.store.merge(a["id"], b["id"], "根拠"),
            lambda: self.store.supersede(a["id"], "同じ事実", "根拠"),
            lambda: self.store.add("fact", "本文", "根拠", dedupe_key="\x00"),
        ):
            with self.assertRaises(brain.InputError):
                call()
        self.assertEqual(self.store.stats(), before)

    def test_source_write_failure_rolls_back_repeat_metadata(self):
        saved = self.store.add("fact", "保存済み", "元")
        with patch.object(self.store, "_event", side_effect=sqlite3.OperationalError("simulated write failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.add("fact", "保存済み", "新しい根拠")
        self.assertEqual(self.store.get(saved["id"]), {key: value for key, value in saved.items() if key != "deduplicated"})

    def test_parallel_processes_share_one_record_per_identity(self):
        shared = Path(self.temp.name) / "concurrent.sqlite3"
        script = """import json, sys, brain
from contextlib import closing
with closing(brain.Store(sys.argv[1])) as store:
    first = store.add('fact', '同時取り込み', '共通の出典')
    second = store.add('fact', '同じ取引', 'receipt', dedupe_key='solana:tx:0')
    print(json.dumps([first['id'], second['id']]))
"""
        processes = [subprocess.Popen([sys.executable, "-c", script, str(shared)], cwd=brain.ROOT,
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
        for process in processes:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(json.loads(output), [1, 2])
        with closing(brain.Store(shared)) as reopened:
            self.assertEqual(reopened.stats()["stored_records"], 2)
            self.assertEqual(reopened.stats()["ingestions"], 16)
            self.assertEqual(reopened.stats()["evidence_sources"], 2)
            self.assertTrue(reopened.audit()["ok"])

    def test_cli_key_stats_and_conflict(self):
        command = [sys.executable, str(brain.ROOT / "brain.py"), "--db", str(self.path)]
        for _ in range(2):
            result = subprocess.run(command + ["add", "fact", "取引A", "--source", "receipt", "--key", "txA"],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["id"], 1)
        conflict = subprocess.run(command + ["add", "fact", "取引B", "--source", "receipt", "--key", "txA"],
                                  capture_output=True, text=True, timeout=15)
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("同じキーの内容が異なります", conflict.stderr)
        result = subprocess.run(command + ["audit"], capture_output=True, text=True, timeout=15)
        self.assertTrue(json.loads(result.stdout)["ok"])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(V1_SCHEMA)
            with db:
                for memory_id, content, source in ((1, "残高　２０ USD", "古い見積り"), (2, "残高 20 USD", "追加の根拠")):
                    db.execute("""INSERT INTO memories(id,kind,content,source,created_at,expires_at)
                        VALUES (?, 'fact', ?, ?, '2026-09-09T00:00:00+00:00', '2026-09-10T00:00:00+00:00')""",
                               (memory_id, content, source))
                    db.execute("INSERT INTO events(memory_id,action,at,details) VALUES (?, 'add', '2026-09-09T00:00:00+00:00', ?)",
                               (memory_id, json.dumps({"source": source})))

    def test_v1_backup_and_migration_preserve_rows_evidence_history(self):
        with closing(brain.Store(self.path)) as store:
            backup = Path(store.migration_backup)
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.stats()["stored_records"], 2)
            self.assertEqual(store.stats()["active_records"], 1)
            self.assertEqual(store.stats()["merged_aliases"], 1)
            self.assertEqual(store.get(2)["content"], "残高 20 USD")
            self.assertEqual(store.get(2)["current_id"], 1)
            self.assertEqual(len(store.get(1)["sources"]), 2)
            self.assertIsNone(store.get(1)["verified_at"])
            self.assertEqual(store.get(1)["expires_at"], "2026-09-10T00:00:00+00:00")
            self.assertEqual(store.show(2)["events"][0]["action"], "add")
            self.assertEqual(store.add("fact", "残高 20 USD", "再送")["id"], 1)
            self.assertEqual(store.stats()["stored_records"], 2)
            self.assertTrue(store.audit()["ok"])
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM memories").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 2)
        with closing(brain.Store(self.path)) as reopened:
            self.assertIsNone(reopened.migration_backup)
        self.assertEqual(len(list(self.path.parent.glob("*.pre-v2-*.sqlite3"))), 1)

    def test_failed_migration_rolls_back_with_original_backup_available(self):
        with patch.object(brain.Store, "_event", side_effect=sqlite3.OperationalError("simulated migration failure")):
            with self.assertRaises(sqlite3.OperationalError):
                brain.Store(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM memories").fetchone()[0], 2)
            self.assertNotIn("merged_into", [row[1] for row in db.execute("PRAGMA table_info(memories)")])
        self.assertEqual(len(list(self.path.parent.glob("*.pre-v2-*.sqlite3"))), 1)
        with closing(brain.Store(self.path)) as retried:
            self.assertTrue(retried.audit()["ok"])


if __name__ == "__main__":
    unittest.main()
