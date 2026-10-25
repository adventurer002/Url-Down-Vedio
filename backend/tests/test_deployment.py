"""Deployment package validation and failure boundaries; no real server changes."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

from deploy import deploy_release as deploy

SHA = 'a' * 40
FILES = {
    'backend/app/main.py': b'# source',
    'backend/alembic.ini': b'[alembic]',
    'backend/alembic/env.py': b'# migration',
    'backend/pyproject.toml': b'[project]',
    'backend/uv.lock': b'version = 1',
    'frontend/dist/index.html': b'<html></html>',
}


def bundle(files=None, sha=SHA, tamper=False, symlink=False):
    files = dict(FILES if files is None else files)
    manifest = {'schema_version': 1, 'commit': sha, 'api_base_url': '/api/v1',
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    if tamper:
        files['backend/app/main.py'] = b'# tampered'
    files['release.json'] = json.dumps(manifest).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if symlink:
            info = tarfile.TarInfo('backend/app/link')
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            archive.addfile(info)
    return stream.getvalue()


def test_bundle_checks_commit_and_every_file_hash():
    files, manifest = deploy.verify_bundle(bundle(), SHA)
    assert files == FILES and manifest['commit'] == SHA
    with pytest.raises(ValueError, match='checksum'):
        deploy.verify_bundle(bundle(tamper=True), SHA)
    with pytest.raises(ValueError, match='manifest'):
        deploy.verify_bundle(bundle(sha='b' * 40), SHA)


@pytest.mark.parametrize('name', ['../escape', '/etc/cron.d/escape',
    'backend/app/../../escape', 'backend/app/.env', 'backend/tests/test_x.py',
    'frontend/dist/../escape', 'storage/files/private'])
def test_bundle_rejects_paths_before_extraction(name):
    with pytest.raises(ValueError):
        deploy.verify_bundle(bundle({**FILES, name: b'bad'}), SHA)


def test_bundle_rejects_links_and_decompression_bomb(monkeypatch):
    with pytest.raises(ValueError):
        deploy.verify_bundle(bundle(symlink=True), SHA)
    monkeypatch.setattr(deploy, 'MAX_BYTES', 5)
    with pytest.raises(ValueError, match='size limit'):
        deploy.verify_bundle(bundle(), SHA)


def test_bundle_rejects_missing_runtime_files():
    files = dict(FILES)
    del files['backend/uv.lock']
    with pytest.raises(ValueError, match='incomplete'):
        deploy.verify_bundle(bundle(files), SHA)


@pytest.fixture
def server(tmp_path, monkeypatch):
    base, state, home = tmp_path / 'app', tmp_path / 'state', tmp_path / 'home'
    (base / 'releases').mkdir(parents=True)
    (state / 'backups').mkdir(parents=True)
    (home / 'incoming').mkdir(parents=True)
    monkeypatch.setattr(deploy, 'BASE', base)
    monkeypatch.setattr(deploy, 'STATE', state)
    monkeypatch.setattr(deploy, 'INCOMING', home / 'incoming')
    monkeypatch.setattr(deploy.os, 'chown', lambda *args: None)
    monkeypatch.setattr(deploy.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=123, pw_gid=123))
    monkeypatch.setattr(deploy.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10 * 1024**3))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'docker':
            kwargs['stdout'].write(b'PGDMP fixture')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(deploy, 'run', run)
    monkeypatch.setattr(deploy.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='success'))
    monkeypatch.setattr(deploy, 'runtime', lambda release, operation: calls.append(operation))
    monkeypatch.setattr(deploy, 'healthy', lambda *args: calls.append('healthy'))
    data = bundle()
    (home / 'incoming' / f'videohub-{SHA}.tar.gz').write_bytes(data)
    return SimpleNamespace(base=base, state=state, calls=calls, digest=hashlib.sha256(data).hexdigest())


def test_deployment_stops_writers_and_backs_up_before_migration(server):
    deploy.deploy(SHA, server.digest)
    calls = server.calls
    backup_index = next(i for i, c in enumerate(calls) if isinstance(c, list) and c[0] == 'docker')
    worker_stop = calls.index(['systemctl', 'stop', deploy.UNITS[1]])
    assert worker_stop < backup_index < calls.index('migrate') < calls.index('healthy')
    assert (server.base / 'current').resolve().parent == server.base / 'releases'
    record = json.loads((server.state / 'deployments.jsonl').read_text().splitlines()[-1])
    assert record['status'] == 'success'
    deploy.deploy(SHA, server.digest)
    assert calls.count('migrate') == 1  # Successful redeploy is idempotent.


def test_checksum_failure_does_not_stop_services(server):
    with pytest.raises(ValueError, match='SHA256'):
        deploy.deploy(SHA, '0' * 64)
    assert not server.calls


def test_migration_failure_leaves_services_stopped_without_blind_rollback(server, monkeypatch):
    old = server.base / 'releases' / 'old'
    old.mkdir()
    _, manifest = deploy.verify_bundle(bundle(), SHA)
    manifest['commit'] = 'b' * 40
    (old / 'release.json').write_text(json.dumps(manifest))
    (server.base / 'current').symlink_to(old)

    def runtime(release, operation):
        server.calls.append(operation)
        if operation == 'migrate':
            raise RuntimeError('migration failed')

    monkeypatch.setattr(deploy, 'runtime', runtime)
    with pytest.raises(RuntimeError, match='migration failed'):
        deploy.deploy(SHA, server.digest)
    assert (server.base / 'current').resolve() == old
    assert not any(isinstance(c, list) and c[:2] == ['systemctl', 'start'] for c in server.calls)
    record = json.loads((server.state / 'deployments.jsonl').read_text().splitlines()[-1])
    assert record['status'] == 'manual_recovery_required'


def test_health_failure_restores_previous_version_only_with_same_migrations(server, monkeypatch):
    old = server.base / 'releases' / 'old'
    old.mkdir()
    _, manifest = deploy.verify_bundle(bundle(), SHA)
    manifest['commit'] = 'b' * 40
    (old / 'release.json').write_text(json.dumps(manifest))
    (server.base / 'current').symlink_to(old)
    attempts = []

    def healthy(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('startup failed')

    monkeypatch.setattr(deploy, 'healthy', healthy)
    with pytest.raises(RuntimeError, match='startup failed'):
        deploy.deploy(SHA, server.digest)
    assert (server.base / 'current').resolve() == old
    record = json.loads((server.state / 'deployments.jsonl').read_text().splitlines()[-1])
    assert record['status'] == 'rolled_back'


def test_failed_worker_stop_prevents_backup_and_migration(server, monkeypatch):
    monkeypatch.setattr(deploy.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='timeout'))
    with pytest.raises(RuntimeError):
        deploy.deploy(SHA, server.digest)
    assert 'migrate' not in server.calls
    assert not any(isinstance(c, list) and c[0] == 'docker' for c in server.calls)
