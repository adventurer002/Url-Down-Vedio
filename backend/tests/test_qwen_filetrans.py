"""Filetrans REST contracts, request isolation and cooperative cancellation."""

import asyncio
import json

import httpx
import pytest

from app.core.exceptions import AppException
from app.modules.clips.timing import locate, transcribe_window
from app.modules.transcript.qwen_filetrans import QwenFiletransASRProvider


class FiletransServer:
    def __init__(self):
        self.requests = []
        self.policy = {
            "upload_host": "https://upload.example.com",
            "upload_dir": "inference/account/test",
            "max_file_size_mb": 25,
            "oss_access_key_id": "temporary-access-key",
            "signature": "temporary-signature",
            "policy": "temporary-policy",
            "x_oss_object_acl": "private",
            "x_oss_forbid_overwrite": "true",
        }
        self.statuses = ["PENDING", "RUNNING", "SUCCEEDED"]
        self.result_url = (
            "https://result.example.com/result.json?Signature=result-secret"
        )
        self.result = {
            "transcripts": [
                {
                    "channel_id": 0,
                    "sentences": [
                        {
                            "begin_time": 250,
                            "end_time": 1250,
                            "text": "The meeting starts at nine.",
                            "language": "en",
                        },
                        {
                            "begin_time": 1500,
                            "end_time": 2000,
                            "text": "Prepare three questions.",
                            "language": "en",
                        },
                    ],
                }
            ]
        }
        self.polling = asyncio.Event()
        self.block_poll = False
        self.submit_timeout = False
        self.poll_retry = False
        self.language = "en"
        self.model = "qwen3-asr-flash-filetrans"
        self.subtask_status = "SUCCEEDED"
        self.results = None

    async def handle(self, request):
        self.requests.append(request)
        if request.url.path == "/api/v1/uploads":
            return httpx.Response(200, json={"data": self.policy})
        if request.url.host == "upload.example.com":
            assert request.method == "POST"
            assert b"temporary-signature" in request.content
            assert b"private-original" not in request.content
            return httpx.Response(200)
        if request.url.path.endswith("/asr/transcription"):
            body = json.loads(request.content)
            assert body["model"] == self.model
            if self.model.startswith("qwen-audio-"):
                assert len(body["input"]["file_urls"]) == 1
                source = body["input"]["file_urls"][0]
                expected = {"channel_id": [0]}
                if self.language:
                    expected["language_hints"] = [self.language]
            else:
                source = body["input"]["file_url"]
                expected = {"channel_id": [0], "enable_words": True}
                if self.language:
                    expected["language"] = self.language
            assert source.startswith("oss://inference/account/test/")
            assert body["parameters"] == expected
            assert request.headers["X-DashScope-OssResourceResolve"] == "enable"
            if self.submit_timeout:
                raise httpx.ReadTimeout("do not expose vendor-secret", request=request)
            return httpx.Response(200, json={"output": {"task_id": "remote-task-id"}})
        if request.url.path.startswith("/api/v1/tasks/"):
            self.polling.set()
            if self.block_poll:
                await asyncio.Event().wait()
            if self.poll_retry:
                self.poll_retry = False
                return httpx.Response(503)
            status = (
                self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            )
            output = {"task_id": "remote-task-id", "task_status": status}
            if self.model.startswith("qwen-audio-"):
                output["results"] = self.results if self.results is not None else [
                    {
                        "transcription_url": self.result_url,
                        "subtask_status": self.subtask_status,
                    }
                ]
            else:
                output["result"] = {"transcription_url": self.result_url}
            return httpx.Response(200, json={"output": output})
        if request.url.host in {
            "result.example.com",
            "dashscope-result-bj.oss-cn-beijing.aliyuncs.com",
        }:
            return httpx.Response(200, json=self.result)
        raise AssertionError("Unexpected Filetrans request")

    def provider(self, **kwargs):
        value = QwenFiletransASRProvider(
            "https://api.example.com/compatible-mode/v1",
            "vendor-secret",
            self.model,
            transport=httpx.MockTransport(self.handle),
            **kwargs,
        )
        value.poll_interval = 0.001
        return value


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "private-original.wav"
    path.write_bytes(b"fixed test audio")
    return path


async def test_upload_poll_results_and_exact_clip_location_without_credentials_crossing_hosts(
    audio,
):
    server = FiletransServer()
    data = await server.provider().transcribe(audio, "en-US")
    assert data.language == "en-US"
    assert data.segments[0] == {
        "start": 0.25,
        "end": 1.25,
        "text": "The meeting starts at nine.",
    }
    assert locate("The meeting starts at nine.", data.segments, 3) == (0.25, 1.25, 1)
    assert (
        len([r for r in server.requests if r.url.path.endswith("/asr/transcription")])
        == 1
    )
    for request in server.requests:
        assert request.headers.get("authorization") == (
            "Bearer vendor-secret" if request.url.host == "api.example.com" else None
        )
    assert audio.exists()  # The source belongs to the Worker, not the cloud adapter.


async def test_punctuation_mode_locates_a_sentence_inside_continuous_speech(audio):
    server = FiletransServer()
    server.language = "zh"
    selected = "第一个方法是先提出问题，开始观看前，写下你想解决的一个真实问题。"
    server.result = {
        "transcripts": [
            {
                "sentences": [
                    {
                        "begin_time": 0,
                        "end_time": 4320,
                        "text": "视频学习要产生价值，需要把看过的内容变成具体行动。",
                    },
                    {"begin_time": 4480, "end_time": 14800, "text": selected},
                    {
                        "begin_time": 14960,
                        "end_time": 23680,
                        "text": "第二个方法是把笔记压缩成下一步行动。",
                    },
                ]
            }
        ]
    }
    # The same continuous speech used to arrive as one large VAD segment.
    # Filetrans's enable_words contract must remain enabled to get punctuation
    # boundaries; proportional estimates cannot supply these exact times.
    transcript = await server.provider().transcribe(audio, "zh")
    assert locate(selected, transcript.segments, 44) == (4.48, 14.8, 1)


@pytest.mark.parametrize("model", [
    "qwen-audio-3.0-asr-flash-filetrans",
    "qwen-audio-3.1-asr-flash-filetrans",
])
@pytest.mark.parametrize("language", ["en-US", None, "und"])
async def test_qwen_audio_filetrans_uses_file_urls_language_hints_and_subtask_results(
    audio, model, language
):
    server = FiletransServer()
    server.model = model
    server.language = "en" if language == "en-US" else None
    data = await server.provider().transcribe(audio, language)
    assert locate("The meeting starts at nine.", data.segments, 3) == (0.25, 1.25, 1)
    assert "authorization" not in server.requests[-1].headers


async def test_qwen_audio_failed_subtask_is_rejected_before_download(audio):
    server = FiletransServer()
    server.model = "qwen-audio-3.1-asr-flash-filetrans"
    server.subtask_status = "FAILED"
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_TASK_FAILED"
    assert not any(r.url.host == "result.example.com" for r in server.requests)


@pytest.mark.parametrize("results", [[], {}, [{}, {}]])
async def test_qwen_audio_invalid_result_list_reports_the_query_stage(audio, results):
    server = FiletransServer()
    server.model = "qwen-audio-3.1-asr-flash-filetrans"
    server.results = results
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_INVALID_RESPONSE"
    assert "查询转写任务" in error.value.message


async def test_retries_poll_reads_without_resubmitting_or_uploading_again(audio):
    server = FiletransServer()
    server.poll_retry = True
    await server.provider().transcribe(audio, "en")
    assert len([r for r in server.requests if r.method == "POST"]) == 2


async def test_lost_submission_ack_is_not_replayed_and_has_a_safe_error(audio):
    server = FiletransServer()
    server.submit_timeout = True
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_SUBMISSION_UNKNOWN"
    assert "vendor-secret" not in error.value.message
    assert (
        len([r for r in server.requests if r.url.path.endswith("/asr/transcription")])
        == 1
    )


@pytest.mark.parametrize("status", ["FAILED", "CANCELED", "UNKNOWN"])
async def test_failed_remote_tasks_do_not_download_results(audio, status):
    server = FiletransServer()
    server.statuses = [status]
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_TASK_FAILED"
    assert not any(r.url.host == "result.example.com" for r in server.requests)


@pytest.mark.parametrize(
    "change",
    [
        {"begin_time": -1},
        {"end_time": 0},
        {"end_time": float("inf")},
        {"begin_time": "250"},
        {"begin_time": True},
        {"text": None},
    ],
)
async def test_invalid_sentence_timestamps_are_rejected(audio, change):
    server = FiletransServer()
    server.result["transcripts"][0]["sentences"][0].update(change)
    # Use a raw serializer for non-finite values, matching a malformed vendor body.
    original = server.handle

    async def handle(request):
        if request.url.host == "result.example.com":
            return httpx.Response(200, content=json.dumps(server.result))
        return await original(request)

    server.handle = handle
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"transcripts": []},
        {"transcripts": [{"sentences": []}]},
        {
            "transcripts": [
                {
                    "sentences": [
                        {"begin_time": 1000, "end_time": 2000, "text": "one"},
                        {"begin_time": 1500, "end_time": 2500, "text": "two"},
                    ]
                }
            ]
        },
    ],
)
async def test_missing_or_overlapping_timestamps_do_not_become_coarse_segments(
    audio, result
):
    server = FiletransServer()
    server.result = result
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/audio",
        "https://localhost/audio",
        "https://169.254.169.254/latest",
        "https://10.1.2.3/object",
    ],
)
@pytest.mark.parametrize("stage", ["upload", "result"])
async def test_provider_supplied_urls_reject_private_networks_before_contact(
    audio, url, stage
):
    server = FiletransServer()
    if stage == "upload":
        server.policy["upload_host"] = url
    else:
        server.result_url = url
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "SSRF_BLOCKED"
    assert not any(str(r.url).startswith(url) for r in server.requests)


async def test_signed_official_http_result_url_is_fetched_over_tls_without_bearer(
    audio,
):
    server = FiletransServer()
    server.result_url = "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result.json?Signature=secret"
    await server.provider().transcribe(audio, "en")
    result_request = server.requests[-1]
    assert result_request.url.scheme == "https"
    assert result_request.url.params["Signature"] == "secret"
    assert "authorization" not in result_request.headers


async def test_redirects_and_oversized_results_are_not_followed_or_loaded(audio):
    server = FiletransServer()
    original = server.handle

    async def redirect(request):
        if request.url.host == "result.example.com":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        return await original(request)

    server.handle = redirect
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_API_ERROR"
    server.handle = original
    server.result["padding"] = "x" * 2000
    provider = server.provider()
    provider.max_response_bytes = 1024
    with pytest.raises(AppException) as error:
        await provider.transcribe(audio, "en")
    assert error.value.code == "ASR_INVALID_RESPONSE"


async def test_upload_size_limits_prevent_a_paid_task(audio):
    server = FiletransServer()
    with pytest.raises(AppException) as error:
        await server.provider(max_upload_mb=0).transcribe(audio, "en")
    assert error.value.code == "ASR_AUDIO_TOO_LARGE" and not server.requests
    server.policy["max_file_size_mb"] = 0
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_AUDIO_TOO_LARGE"
    assert len(server.requests) == 1


async def test_overall_timeout_stops_pending_remote_job_polling(audio):
    server = FiletransServer()
    server.block_poll = True
    with pytest.raises(AppException) as error:
        await server.provider(timeout=0.05).transcribe(audio, "en")
    assert error.value.code == "ASR_TIMEOUT"
    assert server.polling.is_set()


async def test_worker_cooperative_cancellation_stops_filetrans_and_releases_audio(
    audio,
):
    server = FiletransServer()
    server.block_poll = True
    cancelled = False

    async def cancellation_requested():
        return cancelled

    operation = asyncio.create_task(
        transcribe_window(server.provider(), audio, "en", cancellation_requested)
    )
    await asyncio.wait_for(server.polling.wait(), timeout=2)
    cancelled = True
    with pytest.raises(AppException) as error:
        await asyncio.wait_for(operation, timeout=1)
    assert error.value.code == "TASK_CANCELLED"
    assert not any(r.url.host == "result.example.com" for r in server.requests)
    audio.unlink()


@pytest.fixture
def bandwidth_sentence():
    # Sanitized Qwen 3.1 response from BV1Wi33zmEAn, with real millisecond times.
    tokens = [
        (114090, 114370, '比如', ''),
        (114370, 114650, '我把', ''),
        (114650, 114770, '2', ''),
        (114770, 114930, '兆', ''),
        (114930, 115210, '带宽', ''),
        (115210, 115410, '的小', ''),
        (115410, 115810, '水管', ''),
        (115810, 116090, '升级', ''),
        (116090, 116330, '到了', ''),
        (116330, 116530, '8', ''),
        (116530, 116730, '兆', '，'),
        (117010, 117290, '网站', ''),
        (117290, 117530, '加载', ''),
        (117530, 117890, '时长', ''),
        (117890, 118290, '就从', ''),
        (118290, 118450, '3', ''),
        (118450, 118810, '分钟', ''),
        (118810, 119050, '优化', ''),
        (119050, 119410, '到了', ''),
        (119410, 119570, '4', ''),
        (119570, 119730, '0', ''),
        (119730, 119930, '秒', '，'),
        (120170, 120530, '速度', ''),
        (120530, 120810, '优化', ''),
        (120810, 121090, '了', ''),
        (121090, 121330, '4', ''),
        (121330, 121530, '倍', ''),
        (121530, 121770, '多', '。'),
    ]
    return {"begin_time": 114090, "end_time": 121770,
            "text": '比如我把2兆带宽的小水管升级到了8兆，网站加载时长就从3分钟优化到了40秒，速度优化了4倍多。',
            "words": [{"begin_time": start, "end_time": end, "text": text,
                       "punctuation": punctuation}
                      for start, end, text, punctuation in tokens]}


async def test_real_filetrans_clause_ends_at_selected_quote_without_extra_numeric_claim(
    audio, bandwidth_sentence
):
    server = FiletransServer()
    server.model = "qwen-audio-3.1-asr-flash-filetrans"
    server.result = {"transcripts": [{"sentences": [bandwidth_sentence]}]}
    data = await server.provider().transcribe(audio, "en")
    quote = "比如我把2兆带宽的小水管升级到了8兆，网站加载时长就从3分钟优化到了40秒。"
    assert locate(quote, data.segments, 125) == (114.09, 119.93, 1)
    assert data.segments[-1]["text"] == "速度优化了4倍多。"
    with pytest.raises(AppException):
        locate(quote.replace("40秒", "41秒"), data.segments, 125)


@pytest.mark.parametrize("change", [
    {"begin_time": 114000},
    {"begin_time": True},
    {"end_time": float("nan")},
    {"end_time": 123000},
    {"end_time": 114090},
    {"text": "改写"},
    {"punctuation": None},
])
async def test_invalid_word_alignment_is_rejected(audio, bandwidth_sentence, change):
    server = FiletransServer()
    bandwidth_sentence["words"][0].update(change)
    server.result = {"transcripts": [{"sentences": [bandwidth_sentence]}]}
    with pytest.raises(AppException) as error:
        await server.provider().transcribe(audio, "en")
    assert error.value.code == "ASR_INVALID_RESPONSE"


def test_word_boundaries_preserve_decimal_punctuation():
    sentence = {"begin_time": 0, "end_time": 3000, "text": "等待1.5秒，再重试。",
                "words": [
                    {"begin_time": 0, "end_time": 500, "text": "等待"},
                    {"begin_time": 500, "end_time": 1000, "text": "1", "punctuation": "."},
                    {"begin_time": 1000, "end_time": 1500, "text": "5"},
                    {"begin_time": 1500, "end_time": 2000, "text": "秒", "punctuation": "，"},
                    {"begin_time": 2100, "end_time": 3000, "text": "再重试", "punctuation": "。"},
                ]}
    data = QwenFiletransASRProvider.transcript({"transcripts": [{"sentences": [sentence]}]}, "zh")
    assert locate("等待1.5秒", data.segments, 3) == (0, 2, 1)
    with pytest.raises(AppException):
        locate("等待15秒", data.segments, 3)
