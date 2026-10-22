"""Shared cloud API transport: validate every DNS answer and pin the connection IP."""
import asyncio
import socket

import httpcore
import httpx

from app.core.security import check_dns_results


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend):
        self.backend = backend

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            async with asyncio.timeout(min(timeout or 5, 5)):
                results = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
                check_dns_results(results)
                return await self.backend.connect_tcp(results[0][4][0], port, timeout, local_address, socket_options)
        except TimeoutError:
            raise httpcore.ConnectTimeout("DNS/connect timeout") from None
        except OSError:
            raise httpcore.ConnectError("DNS/connect failure") from None

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


def public_transport():
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    transport._pool._network_backend = PublicNetworkBackend(transport._pool._network_backend)
    return transport
