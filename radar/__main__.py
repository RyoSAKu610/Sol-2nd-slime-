import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time

from .config import DEFAULT_CONFIG, DEFAULT_STATE, load
from .report import render
from .runner import Radar
from .service import Lock, control, install
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Local Solana PAPER opportunity radar; no real trading')
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--config', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'once', 'install', 'start', 'stop', 'uninstall', 'status'):
        sub.add_parser(name)
    for name in ('history', 'memory'):
        command = sub.add_parser(name)
        command.add_argument('query', nargs='?', default='')
        command.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    state = args.state.expanduser().resolve()
    config_path = args.config or (state / 'config.json' if (state / 'config.json').exists() else DEFAULT_CONFIG)
    os.umask(0o077)
    try:
        if args.command == 'install':
            result = install(state, config_path)
        elif args.command in ('start', 'stop', 'uninstall'):
            result = control(args.command, state)
        elif args.command == 'status':
            result = control('status')
            status = state / 'status.json'
            if status.exists():
                saved = json.loads(status.read_text())
                result.update({k: saved.get(k) for k in ('generated_iso', 'paper', 'credits', 'memory_events', 'observation_rows', 'runtime_error', 'brain_sync_error')})
                result['report_age_seconds'] = round(time.time() - saved['generated_at'], 1)
                result['feeds'] = [{k: f.get(k) for k in ('name', 'state', 'last_error')} for f in saved['feeds']]
        elif args.command == 'history':
            if not 1 <= args.limit <= 1000:
                raise ValueError('limit must be 1..1000')
            store = Store(state / 'radar.sqlite3')
            try: result = store.history(args.query, args.limit)
            finally: store.close()
        elif args.command == 'memory':
            import brain
            store = brain.Store(state / 'memory.sqlite3')
            try: result = store.select(args.query, limit=args.limit)
            finally: store.close()
        else:
            # GHA collect.yml uses 'once' for a single pass; keep non-interactive (no input()/getpass()).
            lock = Lock(state)
            radar = Radar(state, config_path)
            stopping = threading.Event()
            def terminate(signum, frame): stopping.set()
            signal.signal(signal.SIGTERM, terminate)
            signal.signal(signal.SIGINT, terminate)
            result = {'runtime_error': None}
            try:
                while not stopping.is_set():
                    try:
                        config = radar.tick()
                        radar.store.set('runtime_error', None)
                    except Exception as exc:
                        # No credentials or raw payloads in logs; the type is enough to diagnose here.
                        radar.store.set('runtime_error', type(exc).__name__)
                        config = load(config_path)
                    result = render(state, radar.store, config)
                    if args.command == 'once': break
                    stopping.wait(config['loop_seconds'])
            finally:
                radar.close(); lock.close()
            result = {'completed': args.command, 'report': str(state / 'report.html'), 'runtime_error': result.get('runtime_error')}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == 'once' and result.get('runtime_error') else 0
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
