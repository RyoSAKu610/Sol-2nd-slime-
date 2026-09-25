"""GHA slim snapshot: read-only extraction for the free-tier intermittent series.

PAPER only; no live trading. Reads the running SQLite file in read-only mode
(mode=ro, no WAL/-shm copy, no payload dump) and writes a <1MB summary of
latest token quotes + trade aggregates + feed/credit states only.
Observations.payload full text, wallets detail, memories body, key material,
and WAL auxiliary files are never included.
"""
import argparse
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "radar" / "config.gha.json"
SLIM_LIMIT = 1_048_576

_SAFE_ERROR_PREFIXES = (
    "Network failure: ",
    "HTTP ",
    "Local ",
    "Configured ",
    "NANSEN_API_KEY / key_file not configured",
    "Reported ",
)


def iso(ts=None):
    if ts is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        value = float(ts)
        if not math.isfinite(value):
            return "未取得"
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return "未取得"


def _finite(value):
    try:
        if isinstance(value, bool):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_error(value):
    if not value:
        return ""
    text = str(value)[:200]
    for prefix in _SAFE_ERROR_PREFIXES:
        if text.startswith(prefix):
            return text
    return "redacted_unexpected_error"


def _short(text, limit):
    text = str(text or "")[:limit]
    return text


def _connect_ro(db_path):
    resolved = Path(db_path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"DB not found: {resolved}")
    # Read-only URI: never creates -wal/-shm/-journal side files, never checkpoints.
    uri = "file:" + str(resolved) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection, name):
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def build_slim(db_path, config):
    """Extract whitelisted aggregates only. Never reads observations.payload or WAL files."""
    now = time.time()
    quote_age = config.get("quote_max_age_seconds", 1200)
    connection = _connect_ro(db_path)
    try:
        tokens, feeds = [], []
        if _table_exists(connection, "tokens"):
            try:
                rows = connection.execute(
                    "SELECT address,symbol,score,quote,quote_at,rationale,risks"
                    " FROM tokens ORDER BY score DESC, quote_at DESC LIMIT 60").fetchall()
            except sqlite3.Error:
                rows = []
            for row in rows:
                quote = None
                if row["quote"]:
                    try:
                        quote = json.loads(row["quote"])
                    except (ValueError, TypeError):
                        quote = None
                if not isinstance(quote, dict):
                    quote = {}
                observed = _finite(quote.get("observed_at"))
                fresh = bool(observed is not None and 0 <= now - observed <= quote_age)
                try:
                    rationale = json.loads(row["rationale"]) if row["rationale"] else []
                except (ValueError, TypeError):
                    rationale = []
                try:
                    risks = json.loads(row["risks"]) if row["risks"] else []
                except (ValueError, TypeError):
                    risks = []
                if not isinstance(rationale, list):
                    rationale = []
                if not isinstance(risks, list):
                    risks = []
                tokens.append({
                    "address": _short(row["address"], 64),
                    "symbol": _short(row["symbol"], 24),
                    "score": row["score"] if isinstance(row["score"], int) else 0,
                    "price_usd": _finite(quote.get("price_usd")),
                    "liquidity_usd": _finite(quote.get("liquidity_usd")),
                    "volume_5m_usd": _finite(quote.get("volume_5m_usd")),
                    "quote_at": _finite(row["quote_at"]),
                    "observed_at": observed,
                    "fresh": fresh,
                    "rationale": [_short(item, 200) for item in rationale[:5] if isinstance(item, str)][:5],
                    "risks": [_short(item, 200) for item in risks[:5] if isinstance(item, str)][:5],
                })
        paper = {"mode": "paper", "initial_cash": config.get("initial_cash_usd", 20),
                 "cash": config.get("initial_cash_usd", 20),
                 "realized_pnl": 0.0, "open_count": 0, "closed_count": 0, "recent_closed": []}
        if _table_exists(connection, "trades"):
            try:
                if _table_exists(connection, "kv"):
                    kv = connection.execute(
                        "SELECT value FROM kv WHERE key='initial_cash'").fetchone()
                    if kv:
                        try:
                            initial = json.loads(kv[0])
                            if isinstance(initial, (int, float)) and math.isfinite(initial):
                                paper["initial_cash"] = initial
                        except (ValueError, TypeError):
                            pass
                trade_rows = connection.execute(
                    "SELECT address,symbol,outlay,proceeds,pnl,reason,closed_at"
                    " FROM trades ORDER BY id DESC LIMIT 200").fetchall()
                totals = connection.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(outlay),0) AS outlay,"
                    " COALESCE(SUM(proceeds),0) AS proceeds, COALESCE(SUM(pnl),0) AS pnl,"
                    " SUM(CASE WHEN closed_at IS NULL THEN 1 ELSE 0 END) AS open_n"
                    " FROM trades").fetchone()
                outlay = float(totals["outlay"] or 0)
                proceeds = float(totals["proceeds"] or 0)
                paper["cash"] = round(paper["initial_cash"] - outlay + proceeds, 10)
                paper["realized_pnl"] = float(totals["pnl"] or 0)
                paper["open_count"] = int(totals["open_n"] or 0)
                paper["closed_count"] = int(totals["n"] or 0) - paper["open_count"]
                recent = []
                for row in trade_rows:
                    if row["closed_at"] is None:
                        continue
                    recent.append({
                        "address": _short(row["address"], 64),
                        "symbol": _short(row["symbol"], 24),
                        "pnl": _finite(row["pnl"]),
                        "reason": _short(row["reason"], 24),
                        "closed_at": _finite(row["closed_at"]),
                    })
                    if len(recent) >= 20:
                        break
                paper["recent_closed"] = recent
            except sqlite3.Error:
                pass
        if _table_exists(connection, "feeds"):
            try:
                rows = connection.execute(
                    "SELECT name,state,last_success,last_error,failures,count"
                    " FROM feeds ORDER BY name LIMIT 60").fetchall()
            except sqlite3.Error:
                rows = []
            for row in rows:
                feeds.append({
                    "name": _short(row["name"], 96),
                    "state": _short(row["state"], 32),
                    "last_success_iso": iso(row["last_success"]) if row["last_success"] else "未取得",
                    "last_error": _safe_error(row["last_error"]),
                    "failures": row["failures"] if isinstance(row["failures"], int) else 0,
                    "count": row["count"] if isinstance(row["count"], int) else None,
                })
        credits = {"day": 0.0, "month": 0.0}
        if _table_exists(connection, "credits"):
            try:
                day = datetime.fromtimestamp(now, timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0).timestamp()
                month = datetime.fromtimestamp(now, timezone.utc).replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
                credits["day"] = float(connection.execute(
                    "SELECT COALESCE(SUM(charged),0) FROM credits WHERE at>=?", (day,)).fetchone()[0] or 0)
                credits["month"] = float(connection.execute(
                    "SELECT COALESCE(SUM(charged),0) FROM credits WHERE at>=?", (month,)).fetchone()[0] or 0)
            except sqlite3.Error:
                pass
        counts = {"tokens": len(tokens), "trades_open": paper["open_count"],
                  "trades_closed": paper["closed_count"], "feeds": len(feeds)}
        for table, key in (("observations", "observation_rows"), ("memories", "memory_events")):
            if _table_exists(connection, table):
                try:
                    counts[key] = int(connection.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    counts[key] = 0
            else:
                counts[key] = 0
        # 15-role org page inputs: hypothesis IDs + memory references only.
        # Never include memories.content/source (payload-adjacent), wallets, or key material.
        hypotheses, memories_recent = [], []
        if _table_exists(connection, "memories"):
            try:
                memory_rows = connection.execute(
                    "SELECT event_key,kind,at FROM memories ORDER BY at DESC LIMIT 20").fetchall()
            except sqlite3.Error:
                memory_rows = []
            score_by_address = {t["address"]: t["score"] for t in tokens}
            for memory in memory_rows:
                event_key = _short(memory["event_key"], 128)
                kind = _short(memory["kind"], 24)
                at = _finite(memory["at"])
                memories_recent.append({"event_key": event_key, "kind": kind, "at": at})
                if memory["kind"] == "hypothesis" and len(hypotheses) < 5:
                    mint = (str(event_key).split(":")[-1] or "")[:64]
                    hypotheses.append({
                        "id": event_key,
                        "mint": mint,
                        "score": score_by_address.get(mint),
                        "at": at,
                        "expires_at": (at + 86400) if at is not None else None,
                    })
    finally:
        connection.close()
    slim = {
        "series": "gha-slim",
        "generated_at": now,
        "generated_iso": iso(now),
        "tokens": tokens,
        "paper": paper,
        "feeds": feeds,
        "hypotheses": hypotheses,
        "memories_recent": memories_recent,
        "credits": credits,
        "credit_limits": {"day": config.get("daily_credits"), "month": config.get("monthly_credits")},
        "counts": counts,
        "config_digest": {key: config.get(key) for key in (
            "chain", "mode", "loop_seconds", "quote_max_age_seconds", "entry_score",
            "initial_cash_usd", "position_usd", "reserve_usd", "max_positions",
            "daily_credits", "monthly_credits")},
        "notes": {
            "paper_only": "Solana PAPERのみ。実売買なし。",
            "no_guarantee": "$20→$10,000,000は希望目標で達成予測・収益保証ではない。将来利益は保証しない。",
            "intermittent": config.get(
                "_gha_note", "GHA間欠系列。常駐連続系列とは別系列。"),
            "compatibility": "GHA間欠系列と常駐連続系列の紙成績は互換表示しない別系列。",
            "coverage": "Solana only; DEX latest profiles + active quote set; Nansen first pages only.",
            "freshness": config.get("_gha_quote_note", ""),
            "wal_excluded": "WAL/-shm/-journalの複写なし。observations.payload全文を含めない。",
        },
    }
    raw = json.dumps(slim, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > SLIM_LIMIT:
        slim["tokens"] = slim["tokens"][:30]
        for token in slim["tokens"]:
            token["rationale"] = token["rationale"][:2]
            token["risks"] = token["risks"][:2]
        slim["paper"]["recent_closed"] = slim["paper"]["recent_closed"][:10]
        slim["hypotheses"] = slim["hypotheses"][:5]
        slim["memories_recent"] = slim["memories_recent"][:10]
        raw = json.dumps(slim, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > SLIM_LIMIT:
        slim["tokens"] = slim["tokens"][:10]
        slim["paper"]["recent_closed"] = []
        slim["hypotheses"] = slim["hypotheses"][:3]
        slim["memories_recent"] = []
        raw = json.dumps(slim, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > SLIM_LIMIT:
        raise ValueError(f"slim snapshot exceeds 1MB ({len(raw)} bytes)")
    slim["_bytes"] = len(raw)
    return slim


def write_slim(slim, out_path):
    out = Path(out_path).expanduser()
    if out.parent != Path("."):
        out.parent.mkdir(parents=True, exist_ok=True)
    slim = dict(slim)
    slim.pop("_bytes", None)
    text = json.dumps(slim, ensure_ascii=False, indent=2, allow_nan=False)
    if len(text.encode("utf-8")) > SLIM_LIMIT:
        raise ValueError("slim snapshot exceeds 1MB")
    temporary = out.with_suffix(out.suffix + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, out)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="GHA slim snapshot (PAPER only, read-only, no secrets)")
    parser.add_argument("--db", type=Path, default=Path(".gha_state/radar.sqlite3"))
    parser.add_argument("--out", type=Path, default=Path("slim.json"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).expanduser().read_text())
    if config.get("mode") != "paper" or config.get("chain") != "solana":
        raise ValueError("Only Solana paper mode is implemented; no live trading exists.")
    slim = build_slim(args.db, config)
    out = write_slim(slim, args.out)
    print(json.dumps({"slim": str(out), "bytes": slim.get("_bytes"),
                      "series": "gha-slim", "paper_only": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
