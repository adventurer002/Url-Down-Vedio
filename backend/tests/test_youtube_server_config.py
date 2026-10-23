import asyncio
from contextlib import nullcontext
import json
from unittest.mock import AsyncMock, Mock

from pydantic import SecretStr
import pytest

from app.core.config import Settings, settings
from app.core.exceptions import AppException
from app.modules.video import preflight
from app.modules.video.parsers import runtime, ytdlp, ytdlp_worker
from app.modules.video.parsers.base import ParsedVideo


@pytest.fixture(autouse=True)
def video_settings(monkeypatch):
    monkeypatch.setattr(settings, 'YTDLP_PROXY', SecretStr(''))
    monkeypatch.setattr(settings, 'YTDLP_JS_RUNTIME', 'disabled')
    monkeypatch.setattr(settings, 'YTDLP_DENO_PATH', '')


def test_proxy_configuration_file_and_environment_precedence(tmp_path, monkeypatch):
    file = tmp_path / '.env'
    file.write_text('YTDLP_PROXY=http://operator:secret@127.0.0.1:7890\n')
    monkeypatch.delenv('YTDLP_PROXY', raising=False)
    loaded = Settings(_env_file=file)
    assert loaded.YTDLP_PROXY.get_secret_value() == 'http://operator:secret@127.0.0.1:7890'
    assert 'operator:secret' not in repr(loaded)
    monkeypatch.setenv('YTDLP_PROXY', 'socks5h://proxy.example:1080')
    assert Settings(_env_file=file).YTDLP_PROXY.get_secret_value() == 'socks5h://proxy.example:1080'


@pytest.mark.parametrize('value', ['file:///secret','http://proxy:bad','https://user:secret@proxy:443',
                                   'http://proxy:0','http://proxy/path','http://proxy?token=secret'])
def test_invalid_proxy_never_leaks_credentials(monkeypatch, value):
    monkeypatch.setattr(settings, 'YTDLP_PROXY', SecretStr(value))
    with pytest.raises(AppException) as error: runtime.server_proxy()
    assert error.value.code == 'YTDLP_PROXY_INVALID' and 'secret' not in error.value.message


def test_deno_opt_in_keeps_cookies_and_remote_components_disabled(monkeypatch):
    assert ytdlp.extraction_options(None)['js_runtimes'] == {}
    monkeypatch.setattr(settings, 'YTDLP_JS_RUNTIME', 'deno')
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: '/opt/deno')
    options = ytdlp.extraction_options(None)
    assert options['js_runtimes'] == {'deno':{'path':'/opt/deno'}}
    assert options['remote_components'] == []
    assert options['cookiefile'] is None and options['cookiesfrombrowser'] is None
    assert options['socket_timeout'] == 15 and options['retries'] == 1
    monkeypatch.setattr(runtime.shutil, 'which', lambda _: None)
    with pytest.raises(AppException) as error: runtime.javascript_runtime_options()
    assert error.value.code == 'YTDLP_RUNTIME_UNAVAILABLE'


def test_metadata_download_and_subtitle_share_server_owned_proxy(monkeypatch, tmp_path, capsys):
    from app.modules.download import child
    from app.modules.transcript import subtitle_child
    proxy = 'http://operator:secret@127.0.0.1:7890'
    monkeypatch.setattr(settings, 'YTDLP_PROXY', SecretStr(proxy))
    extractor = Mock(return_value={'error':'TEST_STOP'})
    monkeypatch.setattr(ytdlp_worker, 'extract_metadata', extractor)
    monkeypatch.setattr(ytdlp_worker.sys, 'argv', ['worker','https://example.com/v'])
    ytdlp_worker.main()
    extractor.assert_called_once_with('https://example.com/v', proxy)
    assert 'secret' not in capsys.readouterr().out
    monkeypatch.setattr(child.shutil, 'which', lambda _: '/opt/ffmpeg')
    for module in [child, subtitle_child]:
        guard = Mock(); guard.installed.return_value = nullcontext()
        guard.check_url.side_effect = AppException('TEST_STOP','stop')
        factory = Mock(return_value=guard)
        monkeypatch.setattr(module, 'NetworkGuard', factory)
        with pytest.raises(AppException):
            if module is child: child.download({'url':'https://example.com/v'}, tmp_path, Mock())
            else: subtitle_child.fetch({'url':'https://example.com/v'}, tmp_path, Mock())
        factory.assert_called_once_with(proxy)


@pytest.mark.parametrize('message,code', [
    ("Sign in to confirm you're not a bot secret", 'SOURCE_ACCESS_RESTRICTED'),
    ('HTTP Error 403 Forbidden secret','SOURCE_ACCESS_RESTRICTED'),
    ('HTTP Error 429 Too Many Requests secret','SOURCE_RATE_LIMITED'),
    ('No supported JavaScript runtime could be found','YTDLP_RUNTIME_UNAVAILABLE'),
    ('signature extraction failed','YTDLP_RUNTIME_UNAVAILABLE'),
    ('Network is unreachable user:secret@proxy','SOURCE_NETWORK_UNAVAILABLE')])
def test_platform_error_classification(message, code):
    error = ytdlp.map_extraction_error(RuntimeError(message))
    assert error.code == code and 'secret' not in error.message


@pytest.mark.parametrize('code', ['YTDLP_PROXY_INVALID','YTDLP_RUNTIME_UNAVAILABLE','SOURCE_ACCESS_RESTRICTED',
                                  'SOURCE_RATE_LIMITED','SOURCE_NETWORK_UNAVAILABLE'])
async def test_metadata_protocol_preserves_error_codes(monkeypatch, code):
    process = Mock(returncode=0)
    process.stdout.read = AsyncMock(side_effect=[json.dumps({'error':code}).encode(),b''])
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    monkeypatch.setattr(ytdlp, '_terminate_process_group', AsyncMock())
    with pytest.raises(AppException) as error: await ytdlp.YtDlpParser().parse('https://example.com/v')
    assert error.value.code == code


def test_version_probe_timeout_and_minimum_version(monkeypatch):
    run = Mock(return_value=Mock(returncode=0,stdout='deno 2.2.0\nv8 test'))
    monkeypatch.setattr(preflight.subprocess,'run',run)
    assert preflight.probe_version('/opt/deno',True) is None
    assert run.call_args.kwargs['timeout'] == 3
    run.return_value.stdout = 'deno 2.3.0\nv8 test'
    assert preflight.probe_version('/opt/deno',True) == '2.3.0'


async def test_preflight_fails_on_missing_runtime_and_unsafe_dns(monkeypatch):
    monkeypatch.setattr(preflight,'probe_version',lambda *args:'7.0')
    monkeypatch.setattr(preflight.shutil,'which',lambda _:'/opt/ffmpeg')
    monkeypatch.setattr(preflight,'validate_url',AsyncMock(side_effect=AppException('SSRF_BLOCKED','private',400)))
    parser = Mock(parse=AsyncMock())
    result = await preflight.check('https://youtu.be/ZePPpQLAhcU',parser)
    assert not result['passed'] and result['download_verified'] is False
    assert {item['code'] for item in result['checks']} >= {'YTDLP_RUNTIME_UNAVAILABLE','SSRF_BLOCKED'}
    parser.parse.assert_not_awaited()


async def test_metadata_success_does_not_claim_download_verified(monkeypatch):
    monkeypatch.setattr(settings,'YTDLP_PROXY',SecretStr('http://operator:secret@proxy.example:7890'))
    monkeypatch.setattr(preflight,'javascript_runtime_options',lambda:{'deno':{'path':'/opt/deno'}})
    monkeypatch.setattr(preflight,'probe_version',lambda *args:'2.3.0')
    monkeypatch.setattr(preflight.shutil,'which',lambda _:'/opt/ffmpeg')
    monkeypatch.setattr(preflight,'validate_url',AsyncMock(return_value='https://example.com/v'))
    video=ParsedVideo('youtube','test','fixture',formats=[{'height':480,'vcodec':'avc1','acodec':'mp4a'}])
    result=await preflight.check('https://example.com/v',Mock(parse=AsyncMock(return_value=video)))
    assert result['passed'] and result['metadata_verified'] and result['download_verified'] is False
    assert 'video_480_mp4' in result['presets'] and result['egress'] == 'server_proxy'
    assert 'secret' not in json.dumps(result)
