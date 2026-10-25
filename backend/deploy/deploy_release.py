#!/usr/bin/python3
"""Root-owned, narrowly authorized release switcher for one VideoHub instance.

Install this file separately as /usr/local/sbin/videohub-deploy.
No code from a release is executed as root.
"""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request

BASE = Path('/opt/videohub')
STATE = Path('/var/lib/videohub-deploy')
INCOMING = Path('/home/videohub-deploy/incoming')
UNITS = ['videohub-api.service', 'videohub-worker.service']
TIMERS = ['videohub-cleanup.timer', 'videohub-reconcile.timer']
JOBS = ['videohub-cleanup.service', 'videohub-reconcile.service']
MAX_BYTES = 128 * 1024 * 1024


def verify_bundle(blob, expected_sha):
    """Validate all paths, file hashes and size limits before writing any files."""
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as source:
        for member in source:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or '..' in path.parts
                    or str(path) != member.name or member.name in files
                    or len(files) >= 10000 or member.size < 0):
                raise ValueError('Invalid archive entry')
            if (member.name != 'release.json' and not member.name.startswith((
                    'backend/app/', 'backend/alembic/', 'backend/deploy/', 'frontend/dist/'))
                    and member.name not in {'backend/alembic.ini', 'backend/pyproject.toml',
                        'backend/uv.lock', 'backend/README.md', 'frontend/public/wechat_qr.png'}):
                raise ValueError('Unexpected archive path')
            if any(part.startswith('.') for part in path.parts):
                raise ValueError('Hidden paths are forbidden')
            total += member.size
            if total > MAX_BYTES:
                raise ValueError('Unpacked release exceeds size limit')
            files[member.name] = source.extractfile(member).read()
    manifest = json.loads(files.pop('release.json'))
    if manifest.get('schema_version') != 1 or manifest.get('commit') != expected_sha or manifest.get('api_base_url') != '/api/v1':
        raise ValueError('Release manifest does not match requested commit')
    if manifest.get('files') != {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}:
        raise ValueError('Release file checksum mismatch')
    required = {'backend/app/main.py', 'backend/alembic.ini', 'backend/pyproject.toml',
                'backend/uv.lock', 'frontend/dist/index.html'}
    if not required.issubset(files):
        raise ValueError('Release is incomplete')
    return files, manifest


def run(command, timeout=60, **kwargs):
    return subprocess.run(command, check=True, timeout=timeout, **kwargs)


def runtime(release, operation):
    # Imported dependencies and application code always run as the unprivileged user.
    run(['/usr/sbin/runuser', '-u', 'videohub', '--',
         str(release / 'backend/.venv/bin/python'), '/usr/local/lib/videohub/runtime.py', operation],
        cwd=release / 'backend', timeout=180,
        env={**os.environ, 'PYTHONPATH': str(release / 'backend'), 'HOME': str(BASE)})


def switch(target):
    temporary = BASE / 'current.next'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    temporary.replace(BASE / 'current')


def healthy(started=None):
    deadline = time.monotonic() + 60
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open('http://127.0.0.1:8000/api/v1/health/ready', timeout=2) as response:
                api_ready = response.status == 200
            worker = subprocess.run(['systemctl', 'is-active', '--quiet', UNITS[1]], timeout=5).returncode == 0
            logs = b' ready.' if started is None else subprocess.run(
                ['journalctl', '-u', UNITS[1], '--since', f'@{started}', '--no-pager', '-o', 'cat'],
                timeout=5, capture_output=True, check=True).stdout
            if api_ready and worker and b' ready.' in logs:
                return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(2)
    raise RuntimeError('API readiness or Worker startup check failed')


def record(commit, status, **extra):
    line = {'commit': commit, 'status': status, 'timestamp': int(time.time()), **extra}
    with (STATE / 'deployments.jsonl').open('a') as stream:
        stream.write(json.dumps(line) + '\n')


def migrations(manifest):
    return {name: digest for name, digest in manifest['files'].items()
            if name.startswith('backend/alembic/')}


def deploy(commit, expected_digest):
    if not re.fullmatch('[0-9a-f]{40}', commit) or not re.fullmatch('[0-9a-f]{64}', expected_digest):
        raise ValueError('Expected full commit SHA and package SHA256')
    if shutil.disk_usage(BASE).free < 4 * 1024**3:
        raise ValueError('At least 4 GiB free disk space is required')
    incoming = INCOMING / f'videohub-{commit}.tar.gz'
    fd = os.open(incoming, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('Invalid package file')
        blob = stream.read(MAX_BYTES + 1)
    if len(blob) > MAX_BYTES or hashlib.sha256(blob).hexdigest() != expected_digest:
        raise ValueError('Package SHA256 mismatch')
    files, manifest = verify_bundle(blob, commit)
    current = BASE / 'current'
    if current.exists() and not current.is_symlink():
        raise ValueError('current must be a symlink, not an existing directory')
    old = current.resolve(strict=True) if current.is_symlink() else None
    if old and old.parent != BASE / 'releases':
        raise ValueError('Current release must be inside the managed releases directory')
    if old and json.loads((old / 'release.json').read_text())['commit'] == commit:
        healthy()
        print('This version is already active; no migration or restart performed', flush=True)
        return
    release = BASE / 'releases' / f'{commit}-{time.time_ns()}'
    release.mkdir(parents=True, mode=0o755)
    release.chmod(0o755)
    user = pwd.getpwnam('videohub')
    # Runtime user must not replace the root-owned release directory or manifest.
    for name, data in files.items():
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        for parent in path.parents:
            if parent == release:
                break
            parent.chmod(0o755)
        path.write_bytes(data)
        path.chmod(0o644)
    (release / 'release.json').write_text(json.dumps(manifest))
    venv = release / 'backend/.venv'
    venv.mkdir(mode=0o750)
    os.chown(venv, user.pw_uid, user.pw_gid)
    run(['/usr/sbin/runuser', '-u', 'videohub', '--', '/usr/local/bin/uv', 'sync', '--frozen', '--no-dev'],
        cwd=release / 'backend', timeout=600,
        env={**os.environ, 'HOME': str(BASE), 'UV_PYTHON_INSTALL_DIR': str(BASE / '.python'),
             'UV_PYTHON': '3.14', 'UV_HTTP_TIMEOUT': '30', 'UV_HTTP_RETRIES': '2'})
    runtime(release, 'preflight')
    old_manifest = json.loads((old / 'release.json').read_text()) if old else None
    schema_unchanged = bool(old_manifest and migrations(old_manifest) == migrations(manifest))
    writers_stopped = False
    migration_attempted = False
    migration_completed = False
    try:
        # Stop writers before the backup; worker warm-stop receives up to 1200s.
        record(commit, "stopping_writers", release=str(release))
        run(['systemctl', 'stop', *TIMERS, *JOBS], timeout=180)
        run(['systemctl', 'stop', UNITS[0]], timeout=90)
        run(['systemctl', 'stop', UNITS[1]], timeout=1230)
        for unit in UNITS:
            state = subprocess.run(['systemctl', 'show', unit, '-p', 'Result', '--value'],
                                   timeout=10, capture_output=True, text=True, check=True).stdout.strip()
            if state not in ('success', ''):
                raise RuntimeError('Service failed to stop cleanly; operator must inspect before migration')
        writers_stopped = True
        record(commit, 'backing_up')
        backup = STATE / 'backups' / f'{commit}-{time.time_ns()}.dump'
        with backup.open('xb') as output:
            os.chmod(backup, 0o600)
            run(['docker', 'exec', '1Panel-postgresql-fiYM', 'sh', '-c',
                 'exec pg_dump -U "${POSTGRES_USER:-postgres}" --format=custom --dbname=videohub_prod'],
                timeout=300, stdout=output)
        if backup.stat().st_size == 0:
            raise RuntimeError('Database backup is empty')
        record(commit, 'migrating', backup=str(backup))
        migration_attempted = True
        runtime(release, 'migrate')
        migration_completed = True
        record(commit, "switching_version", release=str(release))
        switch(release)
        started = int(time.time())
        run(['systemctl', 'start', *UNITS], timeout=90)
        healthy(started)
        run(['systemctl', 'start', *TIMERS], timeout=30)
        record(commit, 'success', previous=str(old) if old else None, backup=str(backup))
        print(f'Deployment successful: {commit}; API ready and Worker ready; frontend staged only', flush=True)
    except Exception:
        # Do not let a failed new worker consume tasks while investigating.
        try:
            run(['systemctl', 'stop', *UNITS], timeout=1320)
            for unit in UNITS:
                result = subprocess.run(['systemctl', 'show', unit, '-p', 'Result', '--value'],
                    timeout=10, capture_output=True, text=True, check=True).stdout.strip()
                if result not in ('success', ''):
                    raise RuntimeError('Service termination requires manual inspection')
        except (subprocess.SubprocessError, RuntimeError):
            record(commit, 'manual_recovery_required', reason='Unable to stop services')
            raise
        if old and writers_stopped and (not migration_attempted or (migration_completed and schema_unchanged)):
            switch(old)
            started = int(time.time())
            run(['systemctl', 'start', *UNITS, *TIMERS], timeout=90)
            healthy(started)
            record(commit, 'rolled_back', previous=str(old))
        else:
            record(commit, 'manual_recovery_required', migration_attempted=migration_attempted)
            print('Services left stopped. Keep backup; inspect journal and database before recovery.', file=sys.stderr)
        raise


if __name__ == '__main__':
    os.umask(0o077)
    try:
        if os.geteuid() != 0 or len(sys.argv) != 3:
            raise ValueError('Use sudo videohub-deploy COMMIT PACKAGE_SHA256')
        with (STATE / 'deploy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(*sys.argv[1:])
    except Exception as exc:
        print(f'Deployment failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
