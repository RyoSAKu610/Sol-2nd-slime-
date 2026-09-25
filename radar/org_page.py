"""15-role organization activity page generator (GHA Pages, static HTML only).

PAPER only; no live trading. Reads docs/slim.json (and the same generation
process via radar.gha_snapshot) only. No new external fetch, no secrets,
no wallet display. Missing values render as "未取得".
State is a 2-value mechanical judgment (稼働中/待機) derived from slim.json
signals only; it is not a live process monitor.
"""
import argparse
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ORG_LIMIT = 1_048_576

# Fixed 15 roles: name + one-line role + signal key used for the 2-value state.
ORG_ROLES = [
    {"id": "picard", "name": "ピカード", "role": "統括・方針確定", "signal": "generated", "signal_desc": "slim.json生成時刻の有無"},
    {"id": "heimdall", "name": "ヘイムダル", "role": "広域監視・見張り", "signal": "profiles_ok", "signal_desc": "dex:profilesがok系"},
    {"id": "feynman", "name": "ファインマン", "role": "仕組み解説・根拠整理", "signal": "any_fresh", "signal_desc": "鮮度ありquoteの有無"},
    {"id": "deming", "name": "デミング", "role": "品質確認・検証", "signal": "any_feed_ok", "signal_desc": "いずれかのfeedがok系"},
    {"id": "popper", "name": "ポパー", "role": "反証・リスク指摘", "signal": "tokens_nonempty", "signal_desc": "token一覧の有無"},
    {"id": "shikamaru", "name": "シカマル", "role": "作戦・優先順位付け", "signal": "has_hypotheses", "signal_desc": "仮説の有無"},
    {"id": "liskov", "name": "リスコフ", "role": "設計整合・置換確認", "signal": "config_digest", "signal_desc": "config_digestの有無"},
    {"id": "hamilton", "name": "ハミルトン", "role": "台帳・紙資産管理", "signal": "paper_present", "signal_desc": "paper集計の有無"},
    {"id": "columbo", "name": "コロンボ", "role": "終了取引の追跡", "signal": "has_closed", "signal_desc": "終了取引の有無"},
    {"id": "tintin", "name": "タンタン", "role": "記録・記憶の取材", "signal": "has_memory", "signal_desc": "記憶イベントの有無"},
    {"id": "eco", "name": "エーコ", "role": "命名・記号整理", "signal": "tokens_nonempty", "signal_desc": "token一覧の有無"},
    {"id": "spock", "name": "スポック", "role": "論理・整合性点検", "signal": "generated_feeds", "signal_desc": "生成時刻とfeed一覧の有無"},
    {"id": "argos", "name": "アルゴス", "role": "多眼・価格監視", "signal": "quotes_ok", "signal_desc": "dex:quotes系がok系"},
    {"id": "conway", "name": "コンウェイ", "role": "連携・6時間集計", "signal": "generated", "signal_desc": "slim.json生成時刻の有無"},
    {"id": "kahneman", "name": "カーネマン", "role": "判断・注意喚起", "signal": "no_guarantee", "signal_desc": "非保証注記の有無"},
]


def _esc(value):
    return html.escape(str(value), quote=True)


def _iso_from_epoch(ts):
    try:
        number = float(ts)
        if not math.isfinite(number):
            return "未取得"
        return datetime.fromtimestamp(number, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return "未取得"


def _parse_iso(text):
    if not text or text == "未取得":
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _feed_ok(state):
    return state in ("ok", "ok_empty")


def compute_states(slim):
    """Derive 待機/稼働中 per role from slim.json signals only."""
    feeds = slim.get("feeds") if isinstance(slim.get("feeds"), list) else []
    by_name = {f.get("name"): f for f in feeds if isinstance(f, dict)}
    tokens = slim.get("tokens") if isinstance(slim.get("tokens"), list) else []
    paper = slim.get("paper") if isinstance(slim.get("paper"), dict) else {}
    counts = slim.get("counts") if isinstance(slim.get("counts"), dict) else {}
    hyps = slim.get("hypotheses") if isinstance(slim.get("hypotheses"), list) else []
    mems = slim.get("memories_recent") if isinstance(slim.get("memories_recent"), list) else []
    notes = slim.get("notes") if isinstance(slim.get("notes"), dict) else {}
    generated = slim.get("generated_iso") not in (None, "", "未取得")
    any_feed_ok = any(_feed_ok(f.get("state")) for f in feeds)
    profiles_ok = _feed_ok((by_name.get("dex:profiles") or {}).get("state"))
    quotes_ok = any(_feed_ok(f.get("state")) for name, f in by_name.items() if name.startswith("dex:quotes:"))
    any_fresh = any(isinstance(t, dict) and t.get("fresh") for t in tokens)
    signals = {
        "generated": generated,
        "profiles_ok": profiles_ok,
        "any_fresh": any_fresh,
        "any_feed_ok": any_feed_ok,
        "tokens_nonempty": len(tokens) > 0,
        "has_hypotheses": len(hyps) > 0,
        "config_digest": isinstance(slim.get("config_digest"), dict) and len(slim.get("config_digest")) > 0,
        "paper_present": isinstance(slim.get("paper"), dict) and paper.get("cash") is not None,
        "has_closed": (paper.get("closed_count") or 0) > 0 or len(paper.get("recent_closed") or []) > 0,
        "has_memory": (counts.get("memory_events") or 0) > 0 or len(mems) > 0 or len(hyps) > 0,
        "generated_feeds": generated and len(feeds) > 0,
        "quotes_ok": quotes_ok,
        "no_guarantee": bool(notes.get("no_guarantee")),
    }
    return {role["id"]: ("稼働中" if signals.get(role["signal"]) else "待機") for role in ORG_ROLES}


def _timeline_events(slim):
    events = []  # (sort_ts or None, iso_display, category, text)
    feeds = slim.get("feeds") if isinstance(slim.get("feeds"), list) else []
    for feed in feeds:
        if not isinstance(feed, dict):
            continue
        iso_text = feed.get("last_success_iso") or "未取得"
        ts = _parse_iso(iso_text)
        events.append((ts, iso_text if ts is not None else "未取得", "feeds状態",
                       f"{feed.get('name', '未取得')}：{feed.get('state', '未取得')}（最終成功 {iso_text if ts is not None else '未取得'}）"))
    paper = slim.get("paper") if isinstance(slim.get("paper"), dict) else {}
    recent = paper.get("recent_closed") if isinstance(paper.get("recent_closed"), list) else []
    for trade in recent:
        if not isinstance(trade, dict):
            continue
        ts = trade.get("closed_at") if isinstance(trade.get("closed_at"), (int, float)) else None
        iso_text = _iso_from_epoch(ts) if ts is not None else "未取得"
        symbol = trade.get("symbol") or (str(trade.get("address", ""))[:8] or "未取得")
        pnl = trade.get("pnl")
        pnl_text = str(pnl) if isinstance(pnl, (int, float)) else "未取得"
        events.append((float(ts) if ts is not None and math.isfinite(float(ts)) else None, iso_text, "紙終了",
                       f"{symbol}：損益 {pnl_text}・理由 {trade.get('reason') or '未取得'}"))
    hyps = slim.get("hypotheses") if isinstance(slim.get("hypotheses"), list) else []
    for hyp in hyps:
        if not isinstance(hyp, dict):
            continue
        ts = hyp.get("at") if isinstance(hyp.get("at"), (int, float)) else None
        iso_text = _iso_from_epoch(ts) if ts is not None else "未取得"
        events.append((float(ts) if ts is not None and math.isfinite(float(ts)) else None, iso_text, "新規仮説",
                       f"{hyp.get('id') or '未取得'}（mint {hyp.get('mint') or '未取得'}）"))
    mems = slim.get("memories_recent") if isinstance(slim.get("memories_recent"), list) else []
    for mem in mems:
        if not isinstance(mem, dict) or mem.get("kind") == "hypothesis":
            continue
        ts = mem.get("at") if isinstance(mem.get("at"), (int, float)) else None
        iso_text = _iso_from_epoch(ts) if ts is not None else "未取得"
        events.append((float(ts) if ts is not None and math.isfinite(float(ts)) else None, iso_text, "記憶参照",
                       f"{mem.get('event_key') or '未取得'}（{mem.get('kind') or '未取得'}）"))
    known = sorted([e for e in events if e[0] is not None], key=lambda e: -e[0])
    unknown = [e for e in events if e[0] is None]
    return (known + unknown)[:30]


def build_org_html(slim):
    """Build the static org activity page from a slim.json dict."""
    states = compute_states(slim)
    notes = slim.get("notes") if isinstance(slim.get("notes"), dict) else {}
    paper = slim.get("paper") if isinstance(slim.get("paper"), dict) else {}
    counts = slim.get("counts") if isinstance(slim.get("counts"), dict) else {}
    hyps = slim.get("hypotheses") if isinstance(slim.get("hypotheses"), list) else []
    generated_iso = slim.get("generated_iso") or "未取得"

    role_rows = "".join(
        f"<tr><td>{_esc(role['name'])}</td><td>{_esc(role['role'])}</td>"
        f"<td>{states[role['id']]}</td><td>{_esc(role['signal_desc'])}</td></tr>"
        for role in ORG_ROLES
    )
    events = _timeline_events(slim)
    if events:
        timeline_rows = "".join(
            f"<tr><td>{_esc(iso_text)}</td><td>{_esc(category)}</td><td>{_esc(text)}</td></tr>"
            for _, iso_text, category, text in events
        )
    else:
        timeline_rows = "<tr><td colspan=3>未取得（活動なし・欠測を含む）</td></tr>"
    if hyps:
        hyp_rows = "".join(
            f"<tr><td>{_esc(h.get('id') or '未取得')}</td>"
            f"<td>{_esc(h.get('mint') or '未取得')}</td>"
            f"<td>{_esc(h.get('score') if isinstance(h.get('score'), int) else '未取得')}</td>"
            f"<td>{_esc(_iso_from_epoch(h.get('expires_at')) if isinstance(h.get('expires_at'), (int, float)) else '未取得')}</td></tr>"
            for h in hyps[:5]
        )
    else:
        hyp_rows = "<tr><td colspan=4>未取得（仮説なし・欠測を含む）</td></tr>"

    def _num(value):
        return _esc(value) if isinstance(value, (int, float)) else "未取得"

    doc = f"""<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>15役の活動 (PAPER)</title><main><h1>15役の活動 · GHA間欠共有</h1><p>Solana / PAPER · 実売買なし · 6時間集計</p><p>生成 {_esc(generated_iso)}</p><p>{_esc(notes.get('no_guarantee', '未取得'))}</p><p>{_esc(notes.get('intermittent', '未取得'))}</p><p>{_esc(notes.get('compatibility', '未取得'))}</p><h2>15役一覧</h2><p>状態は「待機 / 稼働中」の2値のみ。slim.jsonの対応信号からの機械判定で、欠測時は待機と表示する。常駐プロセスの監視ではない。</p><table><tr><th>人物</th><th>一言役割</th><th>状態</th><th>判定根拠（対応信号）</th></tr>{role_rows}</table><h2>活動タイムライン</h2><p>紙終了・新規仮説・feeds状態・記憶参照を時刻順に表示。時刻不明は末尾に「未取得」で表示する。</p><table><tr><th>時刻 (UTC)</th><th>種別</th><th>内容</th></tr>{timeline_rows}</table><h2>仮説ボード</h2><p>最新仮説のID・mint・得点・失効日のみ。将来利益の記載なし。失効日は生成から24時間の目安。</p><table><tr><th>ID</th><th>mint</th><th>得点</th><th>失効日 (UTC)</th></tr>{hyp_rows}</table><h2>紙成績</h2><p>終了 {_num(paper.get('closed_count'))} / 保有 {_num(paper.get('open_count'))} / 現金 ${_num(paper.get('cash'))} / 累積損益 ${_num(paper.get('realized_pnl'))}（GHA間欠系列・常駐連続系列と互換表示しない）</p><p>観測行 {counts.get('observation_rows', '未取得')} / 記憶イベント {counts.get('memory_events', '未取得')}（活動累積の記述値）</p><p>{_esc(notes.get('paper_only', '未取得'))} {_esc(notes.get('no_guarantee', '未取得'))}</p><p><a href="index.html">一覧に戻る</a> · <a href="slim.json">slim.json</a>（1MB未満・payload全文なし・WALなし・秘密値なし・ウォレット表示なし）</p></main></html>"""
    raw = doc.encode("utf-8")
    if len(raw) > ORG_LIMIT:
        raise ValueError(f"org page exceeds 1MB ({len(raw)} bytes)")
    return doc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build docs/org.html from docs/slim.json (PAPER only, no secrets)")
    parser.add_argument("--slim", type=Path, default=Path("docs/slim.json"))
    parser.add_argument("--out", type=Path, default=Path("docs/org.html"))
    args = parser.parse_args(argv)
    slim = json.loads(Path(args.slim).expanduser().read_text())
    doc = build_org_html(slim)
    out = Path(args.out).expanduser()
    if out.parent != Path("."):
        out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    temporary.write_text(doc)
    import os
    os.replace(temporary, out)
    print(json.dumps({"org": str(out), "bytes": len(doc.encode("utf-8")),
                      "paper_only": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
