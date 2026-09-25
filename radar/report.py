"""A local escaped HTML report, with no third-party assets or tracking."""
import html
import json
import os
import time
from .store import iso


def snapshot(store, config):
    now = store.now()
    paper = store.paper(config)
    tokens = store.rows('SELECT * FROM tokens ORDER BY score DESC,quote_at DESC LIMIT 60')
    quotes = {t['address']: json.loads(t['quote']) for t in tokens if t['quote']}
    marked, stale = 0, []
    for position in paper['open']:
        q = quotes.get(position['address'])
        if not q or not 0 <= now - q['observed_at'] <= config['quote_max_age_seconds']:
            stale.append(position['address'])
        if q:
            marked += max(0, position['quantity'] * q['price_usd'] * (1-config['slippage_fraction']) * (1-config['fee_fraction']) - config['network_fee_usd'])
    return {'generated_at': now, 'generated_iso': iso(now), 'paper': paper,
        'estimated_equity': paper['cash'] + marked, 'valuation_stale': stale,
        'target_usd': 10000000, 'target_not_a_forecast': True,
        'series': config.get('_gha_series', 'resident'),
        'intermittent_note': config.get('_gha_note'),
        'feeds': store.rows('SELECT * FROM feeds ORDER BY name'), 'credits': store.credit_totals(),
        'credit_limits': {'day': config['daily_credits'], 'month': config['monthly_credits']},
        'tokens': tokens, 'wallets': store.rows('SELECT * FROM wallets ORDER BY added_at'),
        'observation_rows': store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],
        'observation_sightings': store.db.execute('SELECT COALESCE(SUM(seen_count),0) FROM observations').fetchone()[0],
        'memory_events': store.db.execute('SELECT COUNT(*) FROM memories').fetchone()[0],
        'brain_references': store.get('brain_references', []), 'brain_sync_error': store.get('brain_sync_error'),
        'last_completed_tick': store.get('last_completed_tick'), 'clock_warning': store.get('clock_warning'),
        'runtime_error': store.get('runtime_error'),
        'coverage': 'Solana only; DEX latest profiles + active quote set; Nansen first pages only. No social feed connected.'}


def atomic(path, text):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text)
    os.replace(temporary, path)


def render(state, store, config):
    data = snapshot(store, config)
    atomic(state / 'status.json', json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    esc = lambda value: html.escape(str(value), quote=True)
    gha_note = config.get('_gha_note', '')
    gha_html = f"<p class=\"warning\">{esc(gha_note)}</p>" if gha_note else ""
    feed_rows = ''.join(f"<tr><td>{esc(f['name'])}</td><td>{esc(f['state'])}</td><td>{esc(iso(f['last_success']) if f['last_success'] else '未取得')}</td><td>{esc(f['last_error'] or '')}</td></tr>" for f in data['feeds'])
    token_rows = ''
    for token in data['tokens']:
        q = json.loads(token['quote']) if token['quote'] else {}
        fresh = bool(q) and 0 <= data['generated_at'] - q['observed_at'] <= config['quote_max_age_seconds']
        token_rows += f"<tr><td>{esc(token['symbol'] or token['address'][:8])}<small>{esc(token['address'])}</small></td><td>{token['score']}</td><td>{esc(q.get('price_usd', '未取得'))}</td><td>{esc(q.get('liquidity_usd', '不明'))}</td><td>{'取得済' if fresh else '古い / 未取得'}</td><td>{esc(' / '.join(json.loads(token['rationale']))) }<small>{esc(' / '.join(json.loads(token['risks'])))}</small></td></tr>"
    references = ''.join(f"<li>#{esc(e['id'])} {esc(e['content'])}<small>{esc(e['classification'])}</small></li>" for e in data['brain_references'])
    p = data['paper']
    document = f'''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60"><title>Secondary Brain Radar</title>
<style>body{{background:#111722;color:#e4e9f2;font:15px/1.7 system-ui;margin:0;padding:28px}}main{{max-width:1400px;margin:auto}}h1{{font-size:28px}}h2{{font-size:20px;margin-top:32px}}.cards{{display:flex;flex-wrap:wrap;gap:12px}}.card{{background:#1c2838;padding:16px;min-width:160px;border-radius:10px}}b{{font-size:24px;display:block}}small{{display:block;font-size:12px;color:#a9b8c9;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #344156;vertical-align:top}}.scroll{{overflow-x:auto}}.warning{{color:#ffcf7c}}a{{color:#95cfff}}</style><main>
<h1>Secondary Brain · Opportunity Radar</h1><p>Solana / PAPER · 実売買なし · 60秒自動更新</p><p id="heartbeat">生成 {esc(data['generated_iso'])}</p>
<p class="warning">$20 → $10,000,000 は500,000倍の希望目標です。達成予測・収益保証ではありません。ルールの収益優位は未検証、全損もあり得ます。</p>{gha_html}
<div class="cards"><div class="card">シミュレーション資産<b>${data['estimated_equity']:.4f}</b><small>古い価格の保有 {len(data['valuation_stale'])} 件</small></div><div class="card">現金 / 実現損益<b>${p['cash']:.4f}</b><small>${p['realized_pnl']:.6f} / 初期 ${p['initial_cash']}</small></div><div class="card">保有 / 終了<b>{len(p['open'])} / {len(p['closed'])}</b></div><div class="card">観測 / 累計取得<b>{data['observation_rows']} / {data['observation_sightings']}</b><small>記憶イベント {data['memory_events']}</small></div><div class="card">API credit 本日 / 月<b>{data['credits']['day']} / {data['credits']['month']}</b><small>ローカル上限 {config['daily_credits']} / {config['monthly_credits']}。失敗予約も計上。</small></div></div>
<p>買付${config['position_usd']} / 最大{config['max_positions']}件 / 現金下限${config['reserve_usd']}。片道slippage {config['slippage_fraction']*100:.1f}%、手数料{config['fee_fraction']*100:.1f}%、network ${config['network_fee_usd']} は仮定です。</p>
<h2>データ接続</h2><div class="scroll"><table><thead><tr><th>Feed</th><th>状態</th><th>最終成功 UTC</th><th>エラー</th></tr></thead><tbody>{feed_rows}</tbody></table></div><p class="warning">{esc(data['runtime_error'] or data['brain_sync_error'] or data['clock_warning'] or '')}</p>
<h2>候補と観測根拠</h2><p>得点は事前固定のルールです。売買推奨や確率ではありません。受取・netflowは購入や因果を証明しません。</p><div class="scroll"><table><tr><th>Token / mint</th><th>Score</th><th>取得価格 USD</th><th>流動性 USD</th><th>鮮度</th><th>根拠 / 未検証事項</th></tr>{token_rows}</table></div>
<h2>参照した記憶</h2><ul>{references or '<li>該当記憶なし</li>'}</ul><p>{esc(data['coverage'])}</p><p>PCスリープ中・ログアウト中は動作しません。API時刻が無いためDEX価格そのものの鮮度は未確認。SNSやニュース収集は未接続です。</p>
<script>const generated={data['generated_at']};function age(){{const s=Math.max(0,Math.floor(Date.now()/1000-generated));document.getElementById('heartbeat').textContent='生成 '+new Date(generated*1000).toLocaleString()+' · '+s+'秒経過'+(s>180?' ⚠ 更新停止または処理遅延':'');}}age();setInterval(age,1000);</script></main></html>'''
    atomic(state / 'report.html', document)
    return data
