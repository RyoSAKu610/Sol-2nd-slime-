import copy
import importlib.util
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from radar.config import load
from evolution.experiment import (ExperimentConfig, candidate_config, digest,
    load_observations, optimize, run_experiment, simulate, split_points)


def quote(at, price=1, address='A'):
    return {'address': address, 'observed_at': at, 'price_usd': price,
        'liquidity_usd': 100000, 'volume_5m_usd': 5000,
        'buys_5m': 30, 'sells_5m': 10, 'change_5m_pct': 5}


def pair(price='1'):
    return {'chainId': 'solana', 'baseToken': {'address': 'A', 'symbol': 'A'},
        'pairAddress': 'P', 'priceUsd': price, 'liquidity': {'usd': 100000},
        'volume': {'m5': 5000}, 'txns': {'m5': {'buys': 30, 'sells': 10}},
        'priceChange': {'m5': 5}}


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.base = load(Path('/nonexistent/evolution-config'))
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_db(self, records):
        path = self.directory / 'observations.sqlite3'
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE observations(id INTEGER PRIMARY KEY,feed TEXT,at REAL,first_at REAL,payload TEXT,fingerprint TEXT)')
        for first, latest, payload, fingerprint in records:
            db.execute('INSERT INTO observations(feed,at,first_at,payload,fingerprint) VALUES (?,?,?,?,?)',
                ('dex:quotes:0', latest, first, payload, fingerprint))
        db.commit()
        db.close()
        return path

    def test_first_seen_only_and_read_only(self):
        raw = pair()
        path = self.make_db([(100, 999999, json.dumps(raw), digest(raw))])
        before = path.read_bytes()
        points, audit = load_observations(path)
        self.assertEqual(points[0]['observed_at'], 100)
        self.assertEqual(audit['accepted_points'], 1)
        self.assertEqual(before, path.read_bytes())

    def test_missing_time_nonfinite_price_and_mutated_payload_rejected(self):
        valid = pair()
        invalid = pair('NaN')
        raw_nan = pair()
        raw_nan['priceUsd'] = float('nan')
        path = self.make_db([(None, 10, json.dumps(valid), digest(valid)),
            (10, 10, json.dumps(invalid), digest(invalid)),
            (20, 20, json.dumps(valid), 'tx:cannot-verify-original'),
            (30, 30, json.dumps(raw_nan), 'not-an-immutable-hash')])
        points, audit = load_observations(path)
        self.assertEqual(points, [])
        self.assertEqual(audit['missing_first_at'], 1)
        self.assertEqual(audit['invalid_price_or_chain'], 1)
        self.assertEqual(audit['unverifiable_immutable_payload'], 2)

    def test_input_limit_reports_instead_of_optimizing_truncated_sample(self):
        raw = pair()
        path = self.make_db([(i, i, json.dumps(raw), digest(raw)) for i in (1, 2, 3)])
        points, audit = load_observations(path, max_observations=2)
        self.assertEqual(points, [])
        self.assertEqual(audit['error'], 'input_limit_exceeded')

    def test_next_observation_entry_and_costs(self):
        one = simulate([quote(0)], self.base)
        self.assertEqual(one['ending_cash_usd'], 20)
        entered = simulate([quote(0), quote(60)], self.base)
        self.assertEqual(entered['ending_cash_usd'], 18)
        self.assertEqual(entered['unresolved_positions'], 1)
        self.assertLess(entered['terminal_mark_reference_usd'], 20)
        done = simulate([quote(0), quote(60), quote(120, 1.5)], self.base)
        self.assertEqual(done['closed_trades'], 1)
        self.assertGreater(done['ending_cash_usd'], 20)
        self.assertGreater(done['modeled_cost_usd'], 0)

    def test_stale_signal_never_filled(self):
        result = simulate([quote(0), quote(121)], self.base)
        self.assertEqual(result['ending_cash_usd'], 20)
        self.assertEqual(result['stale_buy_signals_dropped'], 1)
        self.assertEqual(result['closed_trades'], 0)

    def test_disappearing_token_is_not_removed_from_loss(self):
        result = simulate([quote(0), quote(60), quote(600, address='B')], self.base)
        self.assertEqual(result['ending_cash_usd'], 18)
        self.assertEqual(result['terminal_mark_reference_usd'], 18)
        self.assertEqual(result['unresolved_positions'], 1)
        self.assertAlmostEqual(result['max_drawdown_fraction_lower_bound'], .1)

    def test_invalid_values_do_not_create_orders(self):
        points = [quote(0, float('nan')), quote(60, 0), quote(120, float('inf'))]
        self.assertEqual(simulate(points, self.base)['ending_cash_usd'], 20)
        with self.assertRaises(ValueError):
            simulate([], dict(self.base, initial_cash_usd=0))

    def test_capital_limit_never_overspent(self):
        points = [quote(at, address=str(n)) for at in (0, 60) for n in range(20)]
        result = simulate(points, self.base)
        self.assertEqual(result['unresolved_positions'], 3)
        self.assertEqual(result['ending_cash_usd'], 14)

    def test_time_split_and_flat_blocks(self):
        points = [quote(i * 3600) for i in range(8 * 24 + 1)]
        groups, _ = split_points(points, ExperimentConfig(min_tokens=1, min_observations_per_block=1), self.base)
        train = [q['observed_at'] for b in groups['train'] for q in b['points']]
        holdout = [q['observed_at'] for b in groups['holdout'] for q in b['points']]
        self.assertLess(max(train), min(holdout))
        self.assertEqual(len(train) + len(holdout), len(points))
        self.assertEqual(len(set(train) & set(holdout)), 0)

    def test_insufficient_data_does_not_import_or_run_optimizer(self):
        with mock.patch('evolution.experiment.optimize', side_effect=AssertionError('must not optimize')):
            report, path, reused = run_experiment([quote(100)], {}, self.base, ExperimentConfig(), self.directory)
        self.assertEqual(report['status'], 'insufficient_data')
        self.assertNotIn('search', report)
        self.assertTrue(path.is_file())
        self.assertFalse(reused)
        saved, same, reused = run_experiment([quote(100)], {}, self.base, ExperimentConfig(), self.directory)
        self.assertTrue(reused)
        self.assertEqual(report['evaluated_at'], saved['evaluated_at'])
        self.assertEqual(path, same)

    def test_two_processes_optimize_once_and_reuse_complete_report(self):
        script = textwrap.dedent('''
            import json, pathlib, sys, time
            import evolution.experiment as e
            from radar.config import load
            output = pathlib.Path(sys.argv[1])
            # Make overlap deterministic without a barrier inside the locked work.
            ready = output / ('ready-' + sys.argv[2])
            ready.touch()
            deadline = time.monotonic() + 10
            while len(list(output.glob('ready-*'))) < 2:
                if time.monotonic() > deadline:
                    raise RuntimeError('second process did not start')
                time.sleep(.01)
            def fake_optimize(*args):
                with (output / 'optimizer-calls').open('a') as stream:
                    stream.write('called\\n')
                time.sleep(.2)
                return {'eligible': False, 'parameters': {}, 'unique_fitness_evaluations': 1,
                        'memoized_evaluations': 0}
            e.optimize = fake_optimize
            # No external dependency is needed to test publication/exclusion.
            e.importlib.metadata.version = lambda package: 'test-only'
            base = load(pathlib.Path('/nonexistent/test-config'))
            base['max_hold_seconds'] = 0
            points = [{'address': 'A', 'observed_at': i * 60, 'price_usd': 1,
                       'liquidity_usd': 100000, 'volume_5m_usd': 5000,
                       'buys_5m': 30, 'sells_5m': 10, 'change_5m_pct': 5} for i in range(100)]
            cfg = e.ExperimentConfig(min_span_days=0, min_observations_per_block=1, min_tokens=1)
            report, path, reused = e.run_experiment(points, {}, base, cfg, output)
            print(json.dumps({'run_id': report['run_id'], 'reused': reused,
                              'markdown': path.with_suffix('.md').exists()}))
        ''')
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.directory), str(i)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
        results = []
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                results.append(json.loads(stdout))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual((self.directory / 'optimizer-calls').read_text().splitlines(), ['called'])
        self.assertEqual(results[0]['run_id'], results[1]['run_id'])
        self.assertEqual(sorted(r['reused'] for r in results), [False, True])
        self.assertTrue(all(r['markdown'] for r in results))

    @unittest.skipUnless(importlib.util.find_spec('deap'), 'DEAP isolated venv required')
    def test_real_deap_repeat_seed_creator_and_zero_trade_penalty(self):
        blocks = [{'points': [quote(i * 60, price=1) for i in range(5)]}] * 3
        experiment = ExperimentConfig(population=8, generations=3)
        state = random.getstate()
        first = optimize(blocks, self.base, experiment)
        second = optimize(blocks, self.base, experiment)
        self.assertEqual(first, second)
        self.assertEqual(random.getstate(), state)
        self.assertFalse(first['eligible'])
        self.assertEqual(first['fitness'], -1e6)
        self.assertGreater(first['memoized_evaluations'], 0)

    @unittest.skipUnless(importlib.util.find_spec('deap'), 'DEAP isolated venv required')
    def test_optimizer_receives_train_only_and_holdout_does_not_change_winner(self):
        train = [{'points': [quote(0), quote(60), quote(120, 2)]}] * 3
        experiment = ExperimentConfig(population=4, generations=1)
        first = optimize(train, self.base, experiment)
        holdout = [quote(1000), quote(1060), quote(1120, .1)]
        simulate(holdout, candidate_config(first['gene'], self.base))
        second = optimize(train, self.base, experiment)
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
