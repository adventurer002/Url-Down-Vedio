"""Publish only checked public build files into the OpenResty-mounted site."""
import hashlib
from pathlib import Path

import pytest

from deploy import deploy_release as deploy


def release(tmp_path, name, files):
    root = tmp_path / name
    for filename, body in files.items():
        path = root / 'frontend/dist' / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    manifest = {'commit': name * 40, 'files': {
        f'frontend/dist/{filename}': hashlib.sha256(body).hexdigest() for filename, body in files.items()}}
    return root, manifest


@pytest.fixture
def site(tmp_path, monkeypatch):
    path = tmp_path / 'website'
    (path / 'index').mkdir(parents=True)
    (path / 'index/index.html').write_text('panel placeholder')
    monkeypatch.setattr(deploy, 'SITE', path)
    return path


def test_site_switches_within_container_mount_and_preserves_initial_files(site, tmp_path):
    root, manifest = release(tmp_path, 'a', {'index.html': b'first', 'assets/main-aaaaaa.js': b'js'})
    # Private backend files must not be copied.
    (root / 'backend').mkdir()
    (root / 'backend/private.env').write_text('not public')
    assert deploy.publish_frontend(root, manifest)
    link = site / 'index'
    assert link.is_symlink() and not link.readlink().is_absolute()
    assert link.resolve().parent == site / '.videohub-releases'
    assert (link / 'index.html').read_bytes() == b'first'
    assert not (link / 'backend').exists()
    initial = next(site.glob('.videohub-initial-index-*'))
    assert (initial / 'index.html').read_text() == 'panel placeholder'


def test_new_version_and_rollback_switch_html_and_keep_hashed_assets(site, tmp_path):
    old, old_manifest = release(tmp_path, 'a', {'index.html': b'old', 'assets/old-aaaaaa.js': b'old js'})
    new, new_manifest = release(tmp_path, 'b', {'index.html': b'new', 'assets/new-bbbbbb.js': b'new js'})
    deploy.publish_frontend(old, old_manifest)
    deploy.publish_frontend(new, new_manifest)
    assert (site / 'index/index.html').read_bytes() == b'new'
    assert (site / 'index/assets/old-aaaaaa.js').read_bytes() == b'old js'
    deploy.publish_frontend(old, old_manifest)
    assert (site / 'index/index.html').read_bytes() == b'old'
    assert (site / 'index/assets/new-bbbbbb.js').is_file()


def test_corrupt_frontend_does_not_change_live_site(site, tmp_path):
    root, manifest = release(tmp_path, 'a', {'index.html': b'expected'})
    (root / 'frontend/dist/index.html').write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum'):
        deploy.publish_frontend(root, manifest)
    assert (site / 'index/index.html').read_text() == 'panel placeholder'


def test_frontend_source_links_are_not_published(site, tmp_path):
    root, manifest = release(tmp_path, 'a', {'index.html': b'expected'})
    index = root / 'frontend/dist/index.html'
    index.rename(index.with_name('other.html'))
    index.symlink_to('other.html')
    with pytest.raises(ValueError, match='links'):
        deploy.publish_frontend(root, manifest)
    assert not (site / 'index').is_symlink()


def test_existing_unmanaged_index_link_is_not_replaced(site, tmp_path):
    index = site / 'index'
    index.rename(site / 'old')
    index.symlink_to('old')
    root, manifest = release(tmp_path, 'a', {'index.html': b'new'})
    with pytest.raises(ValueError, match='outside'):
        deploy.publish_frontend(root, manifest)
    assert index.readlink() == Path('old')


def test_missing_site_keeps_backend_only_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, 'SITE', tmp_path / 'not-created')
    assert deploy.publish_frontend(tmp_path / 'release', {}) is False


def test_failed_first_link_switch_restores_original_directory(site, tmp_path, monkeypatch):
    root, manifest = release(tmp_path, 'a', {'index.html': b'new'})
    original = Path.replace

    def fail_link(path, target):
        if path.name == '.videohub-index.next':
            raise OSError('switch failed')
        return original(path, target)

    monkeypatch.setattr(Path, 'replace', fail_link)
    with pytest.raises(OSError, match='switch failed'):
        deploy.publish_frontend(root, manifest)
    assert not (site / 'index').is_symlink()
    assert (site / 'index/index.html').read_text() == 'panel placeholder'


def test_site_is_published_only_after_backend_health(server, monkeypatch):
    from test_deployment import SHA
    monkeypatch.setattr(deploy, 'publish_frontend', lambda release, manifest: server.calls.append('publish') or True)
    deploy.deploy(SHA, server.digest)
    assert server.calls.index('healthy') < server.calls.index('publish')


def test_site_publication_failure_rolls_back_compatible_backend(server, monkeypatch):
    import json
    from test_deployment import SHA, bundle
    old = server.base / 'releases' / 'old'
    old.mkdir()
    _, manifest = deploy.verify_bundle(bundle(), SHA)
    manifest['commit'] = 'b' * 40
    (old / 'release.json').write_text(json.dumps(manifest))
    (server.base / 'current').symlink_to(old)
    attempts = []

    def publish(release, manifest):
        attempts.append(release)
        if len(attempts) == 1:
            raise OSError('website not writable')
        return True

    monkeypatch.setattr(deploy, 'publish_frontend', publish)
    with pytest.raises(OSError, match='website not writable'):
        deploy.deploy(SHA, server.digest)
    assert attempts[-1] == old
    assert (server.base / 'current').resolve() == old
    assert json.loads((server.state / 'deployments.jsonl').read_text().splitlines()[-1])['status'] == 'rolled_back'


# Reuse the mocked host; no real services, root operations or network requests.
from test_deployment import server
