"""Readiness baseline: latency p50/p95 + error rate for core reads.

Usage: .venv/bin/python scripts/perf_baseline.py [--base http://localhost:8000] [--n 200]
No auth needed (public endpoints only). Fails (exit 1) when p95 > 800ms.
"""

import argparse
import asyncio
import statistics
import sys
import time

import httpx

TARGETS = ["/healthz", "/readyz", "/api/v1/plans"]
P95_BUDGET_MS = 800.0


async def hit(client: httpx.AsyncClient, base: str, path: str) -> tuple[str, float, int]:
    started = time.perf_counter()
    try:
        resp = await client.get(base + path)
        status = resp.status_code
    except Exception:
        status = 0
    return path, (time.perf_counter() - started) * 1000.0, status


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()

    latencies: dict[str, list[float]] = {p: [] for p in TARGETS}
    errors = 0
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for _ in range(args.n):
            results = await asyncio.gather(
                *(hit(client, args.base, p) for p in TARGETS)
            )
            for path, ms, status in results:
                latencies[path].append(ms)
                if status != 200:
                    errors += 1
    total = args.n * len(TARGETS)
    worst_p95 = 0.0
    for path, samples in latencies.items():
        samples.sort()
        p50 = statistics.median(samples)
        p95 = samples[int(len(samples) * 0.95)]
        worst_p95 = max(worst_p95, p95)
        print(f"{path}: n={len(samples)} p50={p50:.1f}ms p95={p95:.1f}ms")
    print(f"errors={errors}/{total}")
    if errors or worst_p95 > P95_BUDGET_MS:
        print("BASELINE FAILED")
        raise SystemExit(1)
    print("BASELINE OK")


if __name__ == "__main__":
    asyncio.run(main())
