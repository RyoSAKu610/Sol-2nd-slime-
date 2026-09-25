"""Experiment: five numeric paper rules; train-only search against fixed baselines.

Only immutable first-seen Dex observations are replayed. Missing prices are never
filled with future values. No HTTP client, key reader, order API, or config writer
is called by this module.
"""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import fcntl
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import statistics
import tempfile
import time

from radar.feeds import number, quote_for
from radar.runner import score_quote

SPACE = {
    'min_liquidity_usd': (25000, 50000, 100000, 200000),
    'min_volume_5m_usd': (1000, 2000, 5000, 10000),
    'entry_score': (45, 55, 65),
    'take_profit_fraction': (0.10, 0.20, 0.30, 0.50),
    'stop_loss_fraction': (0.05, 0.10, 0.15, 0.20),
}
LIMITATIONS = [
    'GAは5個の数値条件の探索。AI全体の自己進化・将来収益の保証ではない。',
    '監視対象として発見された銘柄だけの標本。未発見・上場前・監視開始前を含まず、選択/生存者バイアスは解消していない。',
    'Dex内容が同一の再観測は保存時に集約されるため、初回以外の時系列点は復元できない。欠測を作らず、補間もしない。',
    '初回観測時刻は取得時刻であり価格更新時刻ではない。価格・流動性は売却可能性、MEV、権限リスクを保証しない。',
    'Nansenの後日更新されるラベル・最新token情報は過去に流用しない。今回の探索/固定baselineはDex特徴だけを使用。',
    '区間末・消滅・欠測の未決済資産は下限評価では0ドル。別途参考時価を出すが、売却できたとはみなさない。',
    '検出の次の観測で買いを模擬するため、常駐の即時paper約定とは一致しない。手数料・資金制約・スコア関数は共通。',
    'holdoutの再閲覧後に設定を変更すると未使用評価ではなくなる。保存済み同一実験は再利用し、次の改善には新しい将来期間が必要。',
    '3個の連続時間区間の平均と標準偏差は少数かつ依存した標本の変動幅。独立性・信頼区間・有意な優位を主張しない。',
]


@dataclass(frozen=True)
class ExperimentConfig:
    seed: int = 20260912
    population: int = 16
    generations: int = 4
    train_fraction: float = 0.7
    blocks: int = 3
    min_span_days: float = 7
    min_observations_per_block: int = 50
    min_tokens: int = 3
    min_train_closed_trades: int = 5
    drawdown_penalty: float = 0.5

    def validate(self):
        if not 4 <= self.population <= 128 or not 1 <= self.generations <= 100:
            raise ValueError('population must be 4..128; generations 1..100')
        if self.blocks != 3 or self.train_fraction != 0.7:
            raise ValueError('This protocol fixes three blocks and a 70/30 time split')
        for key, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid experiment number: ' + key)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='seconds')


def load_observations(path, window_days=28, max_observations=100000):
    """Read-only consistent snapshot, retaining dead tokens and rejecting legacy time ambiguity."""
    path = Path(path).expanduser().resolve()
    audit = Counter()
    if not 7 <= window_days <= 365 or not 1 <= max_observations <= 1000000:
        raise ValueError('window_days must be 7..365; max_observations 1..1000000')
    if not path.is_file():
        return [], {'database': str(path), 'error': 'database_missing', 'rows': 0}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    selected = {}
    raw_digest = hashlib.sha256()
    try:
        db.execute('BEGIN')
        columns = {r[1] for r in db.execute('PRAGMA table_info(observations)')}
        if not {'feed', 'first_at', 'payload', 'fingerprint'} <= columns:
            return [], {'database': str(path), 'error': 'first_seen_schema_missing', 'rows': 0}
        maximum = db.execute("SELECT MAX(first_at) FROM observations WHERE feed LIKE 'dex:quotes:%'").fetchone()[0]
        cutoff = (number(maximum) or 0) - window_days * 86400
        query = "SELECT feed,first_at,payload,fingerprint FROM observations WHERE feed LIKE 'dex:quotes:%' AND (first_at>=? OR first_at IS NULL) ORDER BY first_at,id LIMIT ?"
        for row in db.execute(query, (cutoff, max_observations + 1)):
            audit['rows'] += 1
            if audit['rows'] > max_observations:
                return [], dict(audit, error='input_limit_exceeded', max_observations=max_observations,
                    window_days=window_days, database=str(path))
            raw_digest.update(canonical(list(row)).encode())
            at = number(row['first_at'])
            if at is None or at <= 0:
                audit['missing_first_at'] += 1
                continue
            try:
                raw = json.loads(row['payload'])
            except (ValueError, TypeError):
                audit['invalid_json'] += 1
                continue
            try:
                immutable = isinstance(raw, dict) and row['fingerprint'] == digest(raw)
            except (ValueError, TypeError):
                immutable = False
            if not immutable:
                # Only immutable canonical content hashes; tx records can be amended later.
                audit['unverifiable_immutable_payload'] += 1
                continue
            base = raw.get('baseToken')
            address = base.get('address') if isinstance(base, dict) else None
            if not isinstance(address, str) or not address:
                audit['invalid_address'] += 1
                continue
            quote = quote_for(address, [raw], at)
            if quote is None:
                audit['invalid_price_or_chain'] += 1
                continue
            key = (at, address)
            # Highest liquidity within the SAME timestamp only; no future pair selection.
            rank = (quote['liquidity_usd'] or 0, str(quote['pair_address']))
            if key not in selected or rank > selected[key][0]:
                selected[key] = (rank, quote)
        points = [item[1][1] for item in sorted(selected.items())]
        audit['accepted_points'] = len(points)
        audit['same_time_pair_duplicates'] = audit['rows'] - len(points) - sum(
            audit[k] for k in ('missing_first_at', 'invalid_json', 'unverifiable_immutable_payload', 'invalid_address', 'invalid_price_or_chain'))
        return points, dict(audit, database=str(path), source_rows_sha256=raw_digest.hexdigest(),
            window_days=window_days, max_observations=max_observations, window_from=iso(cutoff) if maximum else None)
    finally:
        db.rollback()
        db.close()


def describe(points):
    metrics = {}
    for field in ('price_usd', 'liquidity_usd', 'volume_5m_usd', 'change_5m_pct'):
        values = sorted(q[field] for q in points if number(q.get(field)) is not None)
        n = len(values)
        quart = [values[int((n - 1) * f)] for f in (0.25, 0.5, 0.75)] if n else []
        low, high = (quart[0] - 1.5 * (quart[2] - quart[0]), quart[2] + 1.5 * (quart[2] - quart[0])) if quart else (0, 0)
        metrics[field] = {'valid': n, 'missing': len(points) - n,
            'missing_fraction': (len(points) - n) / len(points) if points else None,
            'min': values[0] if n else None, 'quartiles': quart, 'max': values[-1] if n else None,
            'iqr_outliers': sum(v < low or v > high for v in values)}
    pairs = [(q['liquidity_usd'], q['volume_5m_usd']) for q in points
             if number(q.get('liquidity_usd')) is not None and number(q.get('volume_5m_usd')) is not None]
    corr = None
    if len(pairs) >= 2:
        try:
            corr = statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs])
        except statistics.StatisticsError:
            pass
    directions, gaps = Counter(), []
    previous = {}
    for q in points:
        old = previous.get(q['address'])
        if old:
            change = q['price_usd'] / old['price_usd'] - 1
            directions['up' if change > 0 else 'down' if change < 0 else 'flat'] += 1
            gaps.append(q['observed_at'] - old['observed_at'])
        previous[q['address']] = q
    return {'points': len(points), 'tokens': len(previous), 'fields': metrics,
        'liquidity_volume_correlation': corr, 'adjacent_observed_return_direction': dict(directions),
        'max_gap_seconds': max(gaps, default=None), 'gaps_over_120_seconds': sum(g > 120 for g in gaps),
        'target_note': '観測間価格変化の記述のみ。予測の教師ラベルやGA入力には使用しない。'}


def split_points(points, experiment, base):
    if not points:
        return None, ['利用可能な初回Dex価格観測がありません。']
    begin, end = points[0]['observed_at'], points[-1]['observed_at']
    boundary = begin + (end - begin) * experiment.train_fraction
    groups = {'train': [], 'holdout': []}
    reasons = []
    if end - begin < experiment.min_span_days * 86400:
        reasons.append(f'観測期間{(end - begin) / 86400:.3f}日 < 必要{experiment.min_span_days}日。')
    if len({q['address'] for q in points}) < experiment.min_tokens:
        reasons.append(f'銘柄数が必要{experiment.min_tokens}未満。')
    # Leakage audit: train never contains holdout observations or future labels.
    # Every block starts flat with empty price/pending caches; no position crosses
    # a boundary. Terminal holdings are not sold with a price from the next block.
    for name, start, stop in (('train', begin, boundary), ('holdout', boundary, end + 0.001)):
        width = (stop - start) / experiment.blocks
        for i in range(experiment.blocks):
            left, right = start + i * width, start + (i + 1) * width
            block = [q for q in points if left <= q['observed_at'] < right]
            if len(block) < experiment.min_observations_per_block:
                reasons.append(f'{name}[{i}] 観測{len(block)}件 < 必要{experiment.min_observations_per_block}件。')
            if width <= base['max_hold_seconds']:
                reasons.append(f'{name}[{i}] 区間が最大保有時間以下。')
            groups[name].append({'start': left, 'end': right, 'points': block})
    return groups, reasons


def simulate(points, config):
    """No future fill or removal of disappearing assets; per-block $20 reset."""
    cash = float(config['initial_cash_usd'])
    if not math.isfinite(cash) or cash <= 0:
        raise ValueError('initial_cash_usd must be finite and positive')
    positions, pending, latest, cooldown = {}, {}, {}, {}
    trades, equity, stale_pending = [], [cash], 0
    total_cost = 0.0
    batches = defaultdict(list)
    for q in points:
        if number(q.get('price_usd')) is None or q['price_usd'] <= 0 or number(q.get('observed_at')) is None:
            continue
        batches[q['observed_at']].append(q)

    def proceeds(pos, quote):
        return max(0.0, pos['quantity'] * quote['price_usd'] * (1 - config['slippage_fraction'])
                   * (1 - config['fee_fraction']) - config['network_fee_usd'])

    for at, batch in sorted(batches.items()):
        current = {q['address']: q for q in batch}
        latest.update(current)
        for address, pos in list(positions.items()):
            q = current.get(address)
            if not q or (q['buys_5m'] or 0) + (q['sells_5m'] or 0) <= 0:
                continue
            value = proceeds(pos, q)
            move = value / pos['outlay'] - 1
            reason = 'take_profit' if move >= config['take_profit_fraction'] else 'stop_loss' if move <= -config['stop_loss_fraction'] else 'timeout' if at - pos['opened_at'] >= config['max_hold_seconds'] else None
            if reason:
                cash += value
                total_cost += max(0.0, pos['quantity'] * q['price_usd'] - value)
                trades.append(dict(pos, address=address, closed_at=at, proceeds=value, pnl=value - pos['outlay'], reason=reason))
                del positions[address]
                cooldown[address] = at
        for address, signal_at in sorted(list(pending.items())):
            q = current.get(address)
            if at - signal_at > config['quote_max_age_seconds']:
                stale_pending += 1
                del pending[address]
                continue
            if q is None or at <= signal_at:
                continue
            del pending[address]
            _, _, _, eligible = score_quote(q, {}, config, at)
            if not eligible or address in positions or at - cooldown.get(address, -1e99) < config['cooldown_seconds']:
                continue
            outlay = config['position_usd']
            if len(positions) >= config['max_positions'] or cash - outlay < config['reserve_usd']:
                continue
            spendable = (outlay - config['network_fee_usd']) / (1 + config['fee_fraction'])
            if spendable <= 0:
                continue
            quantity = spendable / (q['price_usd'] * (1 + config['slippage_fraction']))
            positions[address] = {'opened_at': at, 'quantity': quantity, 'outlay': outlay}
            total_cost += outlay - quantity * q['price_usd']
            cash -= outlay
        ranked = sorted(batch, key=lambda q: (-score_quote(q, {}, config, at)[0], q['address']))
        for q in ranked:
            score, _, _, eligible = score_quote(q, {}, config, at)
            address = q['address']
            if eligible and score >= config['entry_score'] and address not in positions and address not in pending and at - cooldown.get(address, -1e99) >= config['cooldown_seconds']:
                pending[address] = at
        # Stale holdings remain owned; their executable-value lower bound is zero.
        value = cash
        for address, pos in positions.items():
            q = latest[address]
            if 0 <= at - q['observed_at'] <= config['quote_max_age_seconds']:
                value += proceeds(pos, q)
        equity.append(value)
    # Do not assume liquidation at a period boundary. Report MTM separately.
    terminal_mark = equity[-1]
    equity.append(cash)
    peak, drawdown = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak if peak > 0 else 0)
    initial = config['initial_cash_usd']
    return {'return_fraction_lower_bound': cash / initial - 1, 'ending_cash_usd': cash,
        'terminal_mark_reference_usd': terminal_mark, 'unresolved_positions': len(positions),
        'closed_trades': len(trades), 'wins': sum(t['pnl'] > 0 for t in trades),
        'max_drawdown_fraction_lower_bound': drawdown, 'modeled_cost_usd': total_cost,
        'stale_buy_signals_dropped': stale_pending, 'unfilled_pending': len(pending), 'trades': trades}


def summarize(results):
    returns = [r['return_fraction_lower_bound'] for r in results]
    return {'mean_return_fraction_lower_bound': statistics.mean(returns),
        'std_return_fraction_lower_bound': statistics.stdev(returns) if len(returns) > 1 else 0,
        'max_drawdown_fraction_lower_bound': max(r['max_drawdown_fraction_lower_bound'] for r in results),
        'closed_trades': sum(r['closed_trades'] for r in results),
        'unresolved_positions': sum(r['unresolved_positions'] for r in results),
        'modeled_cost_usd': sum(r['modeled_cost_usd'] for r in results), 'blocks': results}


def candidate_config(gene, baseline):
    return dict(baseline, **{key: values[index] for (key, values), index in zip(SPACE.items(), gene)})


def optimize(train_blocks, baseline, experiment):
    """DEAP is imported lazily; normal radar and insufficient-data reports need no dependency."""
    from deap import algorithms, base, creator, tools
    random_state = random.getstate()
    random.seed(experiment.seed)
    if not hasattr(creator, 'SecondaryBrainFitness'):
        creator.create('SecondaryBrainFitness', base.Fitness, weights=(1.0,))
        creator.create('SecondaryBrainGene', list, fitness=creator.SecondaryBrainFitness)
    toolbox = base.Toolbox()
    limits = [len(v) - 1 for v in SPACE.values()]
    toolbox.register('individual', tools.initIterate, creator.SecondaryBrainGene,
                     lambda: [random.randrange(high + 1) for high in limits])
    toolbox.register('population', tools.initRepeat, list, toolbox.individual)
    toolbox.register('mate', tools.cxTwoPoint)
    toolbox.register('mutate', tools.mutUniformInt, low=[0] * len(limits), up=limits, indpb=0.3)
    toolbox.register('select', tools.selTournament, tournsize=3)
    cache, attempts = {}, [0]

    def evaluate(individual):
        attempts[0] += 1
        key = tuple(individual)
        if key not in cache:
            config = candidate_config(key, baseline)
            metrics = summarize([simulate(b['points'], config) for b in train_blocks])
            fitness = metrics['mean_return_fraction_lower_bound'] - experiment.drawdown_penalty * metrics['max_drawdown_fraction_lower_bound']
            if metrics['closed_trades'] < experiment.min_train_closed_trades:
                fitness = -1e6  # Zero / sparse trades cannot win by avoiding measurement.
            cache[key] = (fitness, metrics)
        return (cache[key][0],)

    toolbox.register('evaluate', evaluate)
    hall = tools.HallOfFame(1)
    stats = tools.Statistics(lambda item: item.fitness.values[0])
    stats.register('max', max)
    stats.register('mean', statistics.mean)
    try:
        population = toolbox.population(n=experiment.population)
        # Include fixed baseline when its searched values are in the predefined grid.
        initial = [SPACE[key].index(baseline[key]) if baseline[key] in SPACE[key] else 0 for key in SPACE]
        population[0][:] = initial
        _, log = algorithms.eaSimple(population, toolbox, cxpb=0.5, mutpb=0.3,
            ngen=experiment.generations, stats=stats, halloffame=hall, verbose=False)
        winner = tuple(hall[0])
        fitness, metrics = cache[winner]
        return {'gene': list(winner), 'parameters': {key: candidate_config(winner, baseline)[key] for key in SPACE},
            'fitness': fitness, 'train': metrics, 'eligible': fitness > -1e6,
            'evaluation_requests': attempts[0], 'unique_fitness_evaluations': len(cache),
            'memoized_evaluations': attempts[0] - len(cache), 'generation_log': list(log)}
    finally:
        random.setstate(random_state)


def _atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_experiment(points, audit, baseline, experiment, output, synthetic=False):
    # A single output-level process lock covers cache recheck, optimization,
    # prior-holdout inspection, and publication. Concurrent callers reuse the
    # first committed result; different runs cannot both claim first holdout use.
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.experiment.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _run_experiment_locked(points, audit, baseline, experiment, root, synthetic)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _run_experiment_locked(points, audit, baseline, experiment, output, synthetic=False):
    experiment.validate()
    points = sorted(points, key=lambda q: (q['observed_at'], q['address']))
    blocks, insufficient = split_points(points, experiment, baseline)
    if audit.get('error'):
        insufficient.append('入力監査: ' + audit['error'])
    expected_steps = experiment.population * (experiment.generations + 1) * int(len(points) * experiment.train_fraction)
    if expected_steps > 10000000:
        insufficient.append(f'計算量上限: 最大{expected_steps}点評価 > 10,000,000。期間/集団/世代数を減らしてください。')
    manifest = {'protocol': 'secondary-brain-ga-v1', 'data_sha256': digest(points),
        'baseline_sha256': digest(baseline), 'experiment': asdict(experiment),
        'synthetic': synthetic, 'search_space': SPACE,
        'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    # Include shared scoring code in identity so code edits cannot reuse old results.
    import radar.runner
    import radar.feeds
    manifest['shared_code_sha256'] = hashlib.sha256(
        Path(radar.runner.__file__).read_bytes() + Path(radar.feeds.__file__).read_bytes()).hexdigest()
    run_id = digest(manifest)[:20]
    directory = Path(output) / ('demo' if synthetic else 'observed') / run_id
    report_path = directory / 'report.json'
    if report_path.exists():
        saved = json.loads(report_path.read_text())
        if saved.get('manifest') != json.loads(canonical(manifest)):
            raise ValueError('Existing report manifest does not match')
        return saved, report_path, True
    report = {'run_id': run_id, 'evaluated_at': iso(datetime.now(timezone.utc).timestamp()),
        'manifest': manifest, 'status': 'insufficient_data' if insufficient else 'ready',
        'data_kind': 'SYNTHETIC_DEMO_NOT_PERFORMANCE' if synthetic else 'observed_since_monitor_start',
        'audit': audit, 'baseline': baseline, 'insufficient_reasons': insufficient,
        'compute_budget': {'estimated_max_quote_evaluations': expected_steps, 'limit': 10000000},
        'coverage': {'points': len(points), 'tokens': len({q['address'] for q in points}),
                     'from': iso(points[0]['observed_at']) if points else None,
                     'to': iso(points[-1]['observed_at']) if points else None},
        'limitations': LIMITATIONS, 'automatic_application': False,
        'metric_rationale': '手数料込み現金下限リターン平均 − 0.5×最大下落率。未決済・欠測・ゼロ取引を楽観視しない。',
        'leakage_audit': '時刻70/30、各3区間を現金から開始。trainだけ探索。holdoutは固定候補を1回評価。最新ラベル・未来価格・区間を跨ぐ建玉なし。',
        'dependencies': {'python': __import__('platform').python_version()}}
    if blocks:
        report['splits'] = {name: [{'from': iso(b['start']), 'to_exclusive': iso(b['end']), 'points': len(b['points'])} for b in group] for name, group in blocks.items()}
        report['train_eda'] = describe([q for b in blocks['train'] for q in b['points']])
    if not insufficient:
        started = time.monotonic()
        for name in ('deap', 'numpy'):
            report['dependencies'][name] = importlib.metadata.version(name)
        result = optimize(blocks['train'], baseline, experiment)
        report['search'] = result
        if not result['eligible']:
            report['status'] = 'insufficient_trades'
            report['insufficient_reasons'] = ['全候補の学習期間の決済件数が不足。holdoutを使う再探索は行いません。']
        else:
            config = dict(baseline, **result['parameters'])
            candidate = summarize([simulate(b['points'], config) for b in blocks['holdout']])
            fixed = summarize([simulate(b['points'], baseline) for b in blocks['holdout']])
            delta = [a['return_fraction_lower_bound'] - b['return_fraction_lower_bound'] for a, b in zip(candidate['blocks'], fixed['blocks'])]
            mean, std = statistics.mean(delta), statistics.stdev(delta)
            report['holdout'] = {'candidate': candidate, 'fixed_baseline': fixed,
                'cash_baseline': {'return_fraction': 0, 'max_drawdown_fraction': 0, 'closed_trades': 0},
                'paired_delta_mean': mean, 'paired_delta_std': std,
                'interpretation': '差が区間変動以下。明確な差なし。' if abs(mean) <= std else '区間平均に差はあるが、少数・依存標本のため優位は未証明。'}
            prior_holdouts = []
            for previous in directory.parent.glob('*/report.json'):
                old = json.loads(previous.read_text())
                if 'holdout' not in old:
                    continue
                old_range = old['splits']['holdout']
                if old_range[0]['from'] < report['splits']['holdout'][-1]['to_exclusive'] and report['splits']['holdout'][0]['from'] < old_range[-1]['to_exclusive']:
                    prior_holdouts.append(old['run_id'])
            report['holdout']['overlapping_prior_runs'] = prior_holdouts
            report['holdout']['first_local_use'] = not prior_holdouts
            if prior_holdouts:
                report['holdout']['interpretation'] += ' 保存済みの別実験と評価期間が重複しており、新しい未使用期間ではありません。'
            report['status'] = 'synthetic_demo_complete' if synthetic else 'research_candidate_only'
        report['elapsed_seconds'] = time.monotonic() - started
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / 'report.md', render_report(report))
    # JSON is the completed-run marker, published after the human-readable file.
    _atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    return report, report_path, False


def render_report(report):
    lines = ['# DEAP 条件探索の実験記録', '', f"状態: **{report['status']}**", '',
        f"データ: {report['data_kind']}。評価日時: {report['evaluated_at']}",
        f"観測: {report['coverage']['points']}件 / {report['coverage']['tokens']}銘柄。期間: {report['coverage']['from']}〜{report['coverage']['to']}", '',
        '数値条件5個を探索します。常駐ボットの設定変更、実売買は行っていません。', '']
    lines += ['- ' + reason for reason in report['insufficient_reasons']]
    if 'search' in report:
        search = report['search']
        lines += ['', f"探索: {search['unique_fitness_evaluations']}条件を実評価、{search['memoized_evaluations']}回は同じ条件の結果を再利用。", '',
            '|条件|候補|', '|---|---|']
        lines += [f'|{k}|{v}|' for k, v in search['parameters'].items()]
    if 'holdout' in report:
        lines += ['', '|未使用期間・3区間|平均リターン下限 ± 区間標準偏差|最大下落率|決済件数|未決済|', '|---|---:|---:|---:|---:|']
        for name, label in (('candidate', 'GA候補'), ('fixed_baseline', '固定条件')):
            m = report['holdout'][name]
            lines.append(f"|{label}|{m['mean_return_fraction_lower_bound']:.2%} ± {m['std_return_fraction_lower_bound']:.2%}|{m['max_drawdown_fraction_lower_bound']:.2%}|{m['closed_trades']}|{m['unresolved_positions']}|")
        lines += ['|現金保有|0%|0%|0|0|', '', report['holdout']['interpretation']]
    lines += ['', '制約:', ''] + ['- ' + item for item in report['limitations']]
    lines += ['', '再現情報・分割日時・費用・全決済は同じフォルダの `report.json` に保存。', '']
    return '\n'.join(lines)


def synthetic_points(seed=20260912):
    """Eight days of invented quotes, segregated from all observed-data results."""
    rng = random.Random(seed)
    points = []
    start = 1704067200
    for i in range(8 * 86400 // 120 + 1):
        for token in range(3):
            phase = i / 18 + token
            price = (token + 1) * math.exp(0.22 * math.sin(phase) + 0.015 * rng.gauss(0, 1))
            points.append({'chain': 'solana', 'address': f'SYNTHETIC_{token}', 'pair_address': f'DEMO_{token}',
                'symbol': f'DEMO{token}', 'price_usd': price, 'liquidity_usd': 30000 + 70000 * token,
                'volume_5m_usd': 1500 + 3000 * token, 'buys_5m': 30 if math.cos(phase) > 0 else 10,
                'sells_5m': 15, 'change_5m_pct': 5 * math.cos(phase),
                'observed_at': start + i * 120, 'source_price_updated_at': None})
    return points
