"""Anonymous, isolated Douyin browser feasibility probe. Run as an operator.

Chromium runs in an unprivileged network namespace. Its only network transport
is an HTTP CONNECT proxy reached through a Unix socket and a namespace-local
relay. Every destination is resolved and checked before connecting to its IP.
Raw source data and browser errors stay in the private probe directory.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager, suppress
import ipaddress
import json
import os
from pathlib import Path
import select
import signal
import shutil
import tempfile
from types import SimpleNamespace
import socket
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.exceptions import AppException
from app.infrastructure.process_runner import directory_size

MAX_TUNNEL_BYTES = 64 * 1024**2
BROWSER_DOMAINS = ("douyin.com", "douyinstatic.com", "douyinvod.com", "iesdouyin.com", "bytegoofy.com",
                   "bytetos.com", "bytescm.com", "zijieapi.com", "applogcdn.com", "ibytedapm.com",
                   "yhgfb-cn-static.com", "byteimg.com", "bytednsdoc.com", "bytedance.com", "bdstatic.com")


def public_addresses(host):
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("Nonpublic destination")
    return addresses


def bridge(client, upstream, budget, lock):
    started = time.monotonic()
    while time.monotonic() - started < 60:
        readable, _, _ = select.select([client, upstream], [], [], 1)
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            with lock:
                budget[0] += len(data)
                if budget[0] > MAX_TUNNEL_BYTES:
                    raise ValueError("Browser transfer limit")
            (upstream if source is client else client).sendall(data)


class Tunnel(socketserver.StreamRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        upstream = None
        try:
            self.connection.settimeout(5)
            line = self.rfile.readline(4097)
            parts = line.decode("ascii").strip().split()
            if len(line) > 4096 or len(parts) != 3 or parts[0] != "CONNECT":
                raise ValueError("HTTPS CONNECT required")
            destination = urlsplit("//" + parts[1])
            if (not destination.hostname or destination.port != 443 or destination.username or destination.password
                    or destination.path or destination.query or destination.fragment):
                raise ValueError("Invalid tunnel destination")
            if not any(destination.hostname == domain or destination.hostname.endswith("." + domain) for domain in BROWSER_DOMAINS):
                raise ValueError("Unrelated browser destination")
            for _ in range(64):
                header = self.rfile.readline(4097)
                if len(header) > 4096 or not header:
                    raise ValueError("Invalid proxy header")
                if header in {b"\r\n", b"\n"}:
                    break
            else:
                raise ValueError("Too many proxy headers")
            addresses = public_addresses(destination.hostname)
            with self.server.budget_lock:
                self.server.connections += 1
                if self.server.connections > 128:
                    raise ValueError("Browser connection limit")
            # Connecting to the checked IP prevents a second DNS resolution.
            upstream = socket.create_connection((addresses[0], 443), timeout=5)
            self.server.hosts[destination.hostname] += 1
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            bridge(self.connection, upstream, self.server.budget, self.server.budget_lock)
        except Exception as error:
            self.server.errors[type(error).__name__] += 1
            with suppress(OSError):
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        finally:
            if upstream is not None:
                upstream.close()
            self.server.slots.release()


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.settimeout(5)
            upstream.connect(self.server.unix_path)
            self.request.settimeout(5)
            bridge(self.request, upstream, [0], threading.Lock())
        except (OSError, ValueError):
            pass
        finally:
            upstream.close()


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 64


class LocalServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 64


async def inspect_page(args, proxy):
    from playwright.async_api import async_playwright
    details, media_requests, responses = [], [], Counter()
    result = {"browser_started": False, "detail_found": False, "captcha_visible": False,
              "direct_network_blocked": True}
    identifier = args.video_id
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=settings.DOUYIN_BROWSER_EXECUTABLE_PATH or None,
            channel="chromium", headless=True, chromium_sandbox=True,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": "/tmp",
                 "TMPDIR": "/tmp", "LD_LIBRARY_PATH": settings.DOUYIN_BROWSER_LIBRARY_PATH},
            proxy={"server": proxy, "bypass": "<-loopback>"},
            args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        result["browser_started"] = True
        result["browser_version"] = browser.version
        context = await browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 720},
                                            service_workers="block")
        page = await context.new_page()

        async def route_request(route):
            if route.request.resource_type in {"font", "image", "media"}:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_request)

        async def response_received(response):
            parts = urlsplit(response.url)
            if parts.hostname and (parts.hostname == "douyin.com" or parts.hostname.endswith(".douyin.com")):
                responses[str(response.status)] += 1
                if "aweme" in parts.path and "detail" in parts.path:
                    try:
                        body = await response.body()
                        if len(body) <= 3 * 1024**2:
                            details.append(json.loads(body))
                    except Exception:
                        responses["detail_decode_failed"] += 1

        def request_sent(request):
            if request.resource_type == "media":
                media_requests.append(request.url)

        page.on("response", response_received)
        page.on("request", request_sent)
        try:
            await page.goto("https://www.douyin.com/video/" + identifier, wait_until="domcontentloaded", timeout=25000)
            for _ in range(20):
                if any(str((item.get("aweme_detail") or {}).get("aweme_id")) == identifier for item in details):
                    break
                await page.wait_for_timeout(500)
            inline = await page.evaluate("""() => ({router: window._ROUTER_DATA || null,
                render: document.querySelector('script#RENDER_DATA')?.textContent?.slice(0, 3000000) || null})""")
            text = (await page.locator("body").inner_text(timeout=3000))[:50000]
            result["captcha_visible"] = any(word in text for word in ("请完成下列验证", "拖动滑块", "安全验证", "验证码验证"))
            result["login_required_text"] = any(word in text for word in ("登录后继续", "登录后查看"))
            result["final_host"] = urlsplit(page.url).hostname
            result["final_path"] = urlsplit(page.url).path
            result["inline_router_present"] = inline["router"] is not None
            result["inline_render_present"] = bool(inline["render"])
            private = {"responses": details, "inline": inline, "media_requests": media_requests}
            (args.directory / "source-private.json").write_text(json.dumps(private))
            (args.directory / "source-private.json").chmod(0o600)
            result["detail_found"] = any(str((item.get("aweme_detail") or {}).get("aweme_id")) == identifier for item in details)
            result["detail_responses"] = len(details)
            result["media_requests"] = len(media_requests)
        except Exception as error:
            result["page_error_type"] = type(error).__name__
        finally:
            result["responses"] = dict(responses)
            await context.close()
            await browser.close()
    return result


def browser_child(args):
    args.directory = Path(f"/proc/self/fd/{args.proxy_fd}")
    subprocess.run(["mount", "-t", "tmpfs", "-o", "size=128m,nosuid,nodev", "tmpfs", "/tmp"],
                   check=True, timeout=3, capture_output=True)
    # The parent's temporary directory is hidden by the private /tmp mount.
    # Both Playwright's Node driver and Chromium must use this new filesystem.
    os.environ["TMPDIR"] = "/tmp"
    os.environ["HOME"] = "/tmp"
    tempfile.tempdir = "/tmp"
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True, timeout=3, capture_output=True)
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=.5).close()
    except OSError:
        pass
    else:
        raise ValueError("Browser namespace has unexpected direct egress")
    with LocalServer(("127.0.0.1", 0), Relay) as server:
        server.unix_path = str(args.directory / "proxy.sock")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = asyncio.run(inspect_page(args, "http://127.0.0.1:" + str(server.server_address[1])))
        finally:
            server.shutdown()
        (args.directory / "browser-result.json").write_text(json.dumps(result))


@contextmanager
def private_tunnel(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    # /proc keeps the Unix socket address short even for deeply nested task paths.
    address = Path(f"/proc/self/fd/{descriptor}/proxy.sock")
    try:
        with UnixServer(str(address), Tunnel) as server:
            address.chmod(0o600)
            yield server, descriptor
    finally:
        address.unlink(missing_ok=True)
        os.close(descriptor)


def run_probe(args, *, emit=True, separate_session=True):
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.directory.is_symlink() or args.directory.stat().st_mode & 0o077:
        raise ValueError("Probe directory must be private")
    report = {"video_id": args.video_id, "mode": "anonymous_browser_probe", "production_changed": False,
              "download_verified": False, "code": "BROWSER_PROBE_FAILED"}
    with private_tunnel(args.directory) as (server, descriptor):
        server.slots = threading.BoundedSemaphore(64)
        server.hosts, server.errors, server.budget = Counter(), Counter(), [0]
        server.budget_lock = threading.Lock()
        server.connections = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        command = ["unshare", "--user", "--map-current-user", "--net", "--pid", "--fork", "--kill-child=SIGKILL", "--mount-proc", "--keep-caps",
                   sys.executable, "-m", "app.modules.video.parsers.douyin_browser", "browser", "--directory", str(args.directory), "--video-id", args.video_id, "--proxy-fd", str(descriptor)]
        started = time.monotonic()
        with (args.directory / "browser-private.log").open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=separate_session, pass_fds=(descriptor,),
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": str(args.directory),
                     "TMPDIR": str(args.directory), "PYTHONPATH": str(Path(__file__).resolve().parents[4]),
                     "DOUYIN_BROWSER_EXECUTABLE_PATH": settings.DOUYIN_BROWSER_EXECUTABLE_PATH,
                     "DOUYIN_BROWSER_LIBRARY_PATH": settings.DOUYIN_BROWSER_LIBRARY_PATH,
                     "RES_OPTIONS": "attempts:1 timeout:2"})
            try:
                deadline = time.monotonic() + 23
                while process.poll() is None:
                    if time.monotonic() >= deadline or directory_size(args.directory) > 128 * 1024**2:
                        raise subprocess.TimeoutExpired(command, 23)
                    time.sleep(.5)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                report["code"] = "BROWSER_PROBE_TIMEOUT"
            if process.returncode == 0:
                report.update(json.loads((args.directory / "browser-result.json").read_text()))
                report["code"] = "OK" if report["detail_found"] else "SOURCE_ACCESS_RESTRICTED" if report["captcha_visible"] else "SOURCE_FORMATS_UNAVAILABLE"
        report.update(seconds=round(time.monotonic() - started, 3), browser_bytes=server.budget[0],
                      requested_hosts=dict(server.hosts), tunnel_errors=dict(server.errors))
        server.shutdown()
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if emit:
        print(json.dumps(report, ensure_ascii=False))
    return report


def browser_detail(identifier):
    if (sys.platform != "linux" or not shutil.which("unshare") or not shutil.which("ip")
            or not Path(settings.DOUYIN_BROWSER_EXECUTABLE_PATH).is_file()):
        raise AppException("SOURCE_BROWSER_UNAVAILABLE", "服务器抖音浏览器运行时尚未就绪", 503)
    with tempfile.TemporaryDirectory(prefix="vhdy-", dir=os.environ.get("VIDEOHUB_BROWSER_WORK_DIRECTORY")) as directory:
        args = SimpleNamespace(directory=Path(directory), video_id=identifier, report=Path(directory) / "report.json")
        report = run_probe(args, emit=False, separate_session=False)
        if report["code"] != "OK":
            if report["code"] == "SOURCE_ACCESS_RESTRICTED":
                from app.modules.video.parsers.douyin import restricted
                raise restricted()
            code = "PARSE_TIMEOUT" if report["code"] == "BROWSER_PROBE_TIMEOUT" else "SOURCE_BROWSER_UNAVAILABLE" if not report.get("browser_started") else "SOURCE_FORMATS_UNAVAILABLE"
            raise AppException(code, "抖音浏览器取源未完成，可稍后重试或上传文件", 503)
        payload = json.loads((args.directory / "source-private.json").read_text())
        detail = next((item.get("aweme_detail") for item in payload["responses"]
                       if isinstance(item, dict) and str((item.get("aweme_detail") or {}).get("aweme_id")) == identifier), None)
        if not detail:
            raise AppException("SOURCE_FORMATS_UNAVAILABLE", "抖音浏览器未返回当前视频的有效数据", 502)
        return detail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["run", "browser"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--video-id", default="7700513046734006379")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--proxy-fd", type=int)
    args = parser.parse_args()
    if not args.video_id.isdecimal() or not 1 <= len(args.video_id) <= 24:
        parser.error("A numeric public video ID is required")
    if args.mode == "browser":
        browser_child(args)
    else:
        if args.report is None:
            parser.error("--report is required")
        run_probe(args)


if __name__ == "__main__":
    main()
