"""Run with python -m evolution run (observed data) or demo (invented data)."""
import argparse
import json
from pathlib import Path
import sys

from radar.config import DEFAULT_STATE, load
from .experiment import ExperimentConfig, load_observations, run_experiment, synthetic_points


def main(argv=None):
    parser = argparse.ArgumentParser(description='DEAPによるオフラインpaper条件探索。常駐設定への反映なし。')
    parser.add_argument('command', choices=['run', 'demo'])
    parser.add_argument('--database', type=Path, default=DEFAULT_STATE / 'radar.sqlite3')
    parser.add_argument('--config', type=Path, default=DEFAULT_STATE / 'config.json')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'runs')
    parser.add_argument('--seed', type=int, default=20260912)
    parser.add_argument('--population', type=int, default=16)
    parser.add_argument('--generations', type=int, default=4)
    parser.add_argument('--window-days', type=int, default=28)
    parser.add_argument('--max-observations', type=int, default=100000)
    args = parser.parse_args(argv)
    try:
        baseline = load(args.config)
        # Authentication configuration is never copied into experiment artifacts.
        for field in ('key_file', 'key_variable', 'wallets'):
            baseline.pop(field, None)
        experiment = ExperimentConfig(seed=args.seed, population=args.population, generations=args.generations)
        if args.command == 'demo':
            points = synthetic_points(args.seed)
            audit = {'source': 'generated synthetic quotes; no real history', 'rows': len(points)}
        else:
            points, audit = load_observations(args.database, args.window_days, args.max_observations)
        report, path, reused = run_experiment(points, audit, baseline, experiment, args.output, args.command == 'demo')
        print(json.dumps({'status': report['status'], 'report': str(path.with_suffix('.md')),
            'details': str(path), 'reused_saved_run': reused,
            'insufficient_reasons': report['insufficient_reasons']}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, ImportError) as exc:
        print('実験を停止: ' + str(exc), file=sys.stderr)
        print('依存がない場合: evolution/.venv/bin/python -m pip install -r evolution/requirements.txt', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
