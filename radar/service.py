"""User launchd lifecycle. Copies code outside Documents to avoid macOS TCC."""
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
from .config import ROOT, load

LABEL = 'local.secondary-brain.radar'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
DOMAIN = 'gui/' + str(os.getuid())


class Lock:
    def __init__(self, state):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (state / 'radar.lock').open('a+')
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise ValueError('Radar is already running for this state directory') from None
        self.file.seek(0); self.file.truncate(); self.file.write(str(os.getpid())); self.file.flush()

    def close(self):
        self.file.close()


def launch(*args, check=True):
    result = subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError('launchctl ' + args[0] + ' failed: ' + result.stderr.strip())
    return result


def stopped_lock(state):
    # bootout removes the job before an in-flight HTTP timeout has necessarily ended.
    # launchd ExitTimeOut is 30s; wait for the authoritative flock, never a PID file.
    deadline = time.monotonic() + 35
    while True:
        try:
            return Lock(state)
        except ValueError:
            if time.monotonic() >= deadline:
                raise ValueError('Radar process did not release its lock after stop') from None
            time.sleep(0.1)


def canonical_memory(state):
    """Move through SQLite backup then replace source with a link; never overwrite."""
    source = ROOT / '.brain' / 'memory.sqlite3'
    target = state / 'memory.sqlite3'
    if source.is_symlink():
        if source.resolve() != target.resolve():
            raise ValueError('Project brain already points to a different store')
        return
    if source.exists() and target.exists():
        raise ValueError('Both brain databases exist; refusing to overwrite or silently fork memory')
    source.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        backup = source.with_name('memory.before-radar-' + str(time.time_ns()) + '.sqlite3')
        original, destination = sqlite3.connect(source), sqlite3.connect(target)
        try:
            original.backup(destination)
        finally:
            destination.close()
            original.close()
        # Connections must be closed before rename; retain the original file.
        source.rename(backup)
        for suffix in ('-wal', '-shm'):
            auxiliary = Path(str(source) + suffix)
            if auxiliary.exists():
                auxiliary.rename(Path(str(backup) + suffix))
    else:
        import brain
        store = brain.Store(target); store.close()
    source.symlink_to(target)


def install(state, config_path):
    if sys.platform != 'darwin':
        raise ValueError('launchd installation requires macOS')
    # Stop before replacing any deployed code. Existing state and credentials persist.
    launch('bootout', DOMAIN + '/' + LABEL, check=False)
    config = load(config_path)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    lock = stopped_lock(state)
    try:
        canonical_memory(state)
        app = state / 'app'
        app.mkdir(exist_ok=True)
        shutil.copytree(ROOT / 'radar', app / 'radar', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', 'config.local.json', 'tests'))
        shutil.copy2(ROOT / 'brain.py', app / 'brain.py')
        runtime_config = state / 'config.json'
        # Reinstall updates code but preserves the user's runtime configuration.
        if not runtime_config.exists():
            runtime_config.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
            os.chmod(runtime_config, 0o600)
        PLIST.parent.mkdir(exist_ok=True)
        payload = {'Label': LABEL,
            'ProgramArguments': [sys.executable, '-m', 'radar', '--state', str(state), '--config', str(runtime_config), 'run'],
            'WorkingDirectory': str(app), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 30, 'ExitTimeOut': 30,
            'StandardOutPath': str(state / 'launchd.log'), 'StandardErrorPath': str(state / 'launchd-error.log'),
            'EnvironmentVariables': {'PYTHONUNBUFFERED': '1'}, 'Umask': 0o077}
        PLIST.write_bytes(plistlib.dumps(payload))
    finally:
        lock.close()
    launch('bootstrap', DOMAIN, str(PLIST))
    return {'installed': str(PLIST), 'state': str(state), 'report': str(state / 'report.html')}


def control(action, state=None):
    if action == 'stop':
        launch('bootout', DOMAIN + '/' + LABEL, check=False)
    elif action == 'start':
        if not PLIST.exists():
            raise ValueError('Install the radar first')
        if launch('print', DOMAIN + '/' + LABEL, check=False).returncode:
            launch('bootstrap', DOMAIN, str(PLIST))
        else:
            launch('kickstart', DOMAIN + '/' + LABEL)
    elif action == 'uninstall':
        launch('bootout', DOMAIN + '/' + LABEL, check=False)
        PLIST.unlink(missing_ok=True)
    if action in ('stop', 'uninstall') and state is not None:
        stopped_lock(state).close()
    result = launch('print', DOMAIN + '/' + LABEL, check=False)
    lines = [line.strip() for line in result.stdout.splitlines() if any(line.strip().startswith(key) for key in ('state =', 'pid =', 'last exit code =', 'runs ='))]
    return {'loaded': result.returncode == 0, 'launchd': lines,
            'state_retained': True, 'plist_exists': PLIST.exists()}
