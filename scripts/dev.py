"""Local V1 supervisor: API + mandatory Solo Worker + Vite; optional Compose dependencies."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Start the full local VideoHub V1 stack (Ctrl+C for warm shutdown).")
    parser.add_argument('--docker', action='store_true', help='Start PostgreSQL/Redis with Docker Compose first')
    parser.add_argument('--skip-install', action='store_true', help='Skip uv sync / pnpm install after initial setup')
    args = parser.parse_args()
    for binary in ['uv', 'pnpm', 'ffmpeg'] + (['docker'] if args.docker else []):
        if not shutil.which(binary):
            parser.error(f'Missing required executable: {binary}')
    for port in [8000, 5173]:
        with socket.socket() as sock:
            sock.settimeout(.4)
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                parser.error(f'Port {port} is already in use. Stop the previous local API/frontend first; no existing process was killed.')
    # A manually started Solo worker has no listening port. Avoid doubling workers on 8GB machines.
    if shutil.which('ps'):
        listing = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=3)
        existing = [line.split(maxsplit=1)[0] for line in listing.stdout.splitlines()
                    if 'app.infrastructure.queue.celery_app' in line and ' worker ' in line]
        if existing:
            parser.error('A VideoHub Celery worker is already running (PID ' + ', '.join(existing) + '). Stop its terminal before starting the supervised stack.')
    if not (ROOT / '.env').exists():
        shutil.copyfile(ROOT / '.env.example', ROOT / '.env')
    env = dict(os.environ)
    def run(command, cwd=ROOT, timeout=120):
        subprocess.run(command, cwd=cwd, env=env, check=True, timeout=timeout)
    if args.docker:
        run(['docker', 'compose', 'up', '-d', '--wait', '--wait-timeout', '60'], timeout=90)
    if not args.skip_install:
        run(['uv', 'sync', '--locked'], ROOT / 'backend', 600)
        run(['pnpm', 'install', '--frozen-lockfile'], ROOT / 'frontend', 600)
    run(['uv', 'run', 'alembic', 'upgrade', 'head'], ROOT / 'backend', 60)
    runtime = ROOT / '.runtime'; runtime.mkdir(exist_ok=True)
    children = []
    def start(name, command, cwd):
        with (runtime / f'{name}.log').open('w') as log:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log, start_new_session=True)
        children.append((name, process))
        return process
    def wait_until(check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name, process in children:
                if process.poll() is not None:
                    raise RuntimeError(f'{name} exited; inspect .runtime/{name}.log')
            try:
                if check(): return
            except (OSError, ValueError):
                pass
            time.sleep(.2)
        raise RuntimeError('Startup timed out; inspect .runtime/*.log (PG/Redis and Worker must be ready).')
    def ready():
        with urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=2) as response:
            return json.load(response)['data']['status'] == 'ready'
    def front_ready():
        with urllib.request.urlopen('http://127.0.0.1:5173', timeout=2) as response:
            return response.status == 200
    try:
        start('api', ['uv', 'run', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000'], ROOT / 'backend')
        wait_until(ready)
        start('worker', ['uv', 'run', 'celery', '-A', 'app.infrastructure.queue.celery_app:celery_app', 'worker', '--pool=solo', '-c', '1', '--loglevel=info'], ROOT / 'backend')
        wait_until(lambda: ' ready.' in (runtime / 'worker.log').read_text(errors='replace')[-65536:])
        start('frontend', ['pnpm', 'dev'], ROOT / 'frontend')
        wait_until(front_ready)
        print('Ready: http://127.0.0.1:5173 | Swagger: http://127.0.0.1:8000/docs', flush=True)
        print('API / PostgreSQL / Redis / Solo Worker / Vite are running. Logs: .runtime/*.log. Ctrl+C to stop.', flush=True)
        while True:
            for name, process in children:
                if process.poll() is not None: raise RuntimeError(f'{name} stopped; inspect .runtime/{name}.log')
            time.sleep(.5)
    except KeyboardInterrupt:
        print('\nStopping. The Solo Worker finishes its current bounded task before exiting.', flush=True)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        # Warm shutdown must keep the watchdog alive until its independent download process exits.
        for name, process in reversed(children):
            if process.poll() is not None: continue
            if name == 'worker': process.send_signal(signal.SIGTERM)
            else: os.killpg(process.pid, signal.SIGTERM)
        for name, process in reversed(children):
            try:
                process.wait(timeout=930 if name == 'worker' else 45)
            except subprocess.TimeoutExpired:
                if name == 'worker':
                    print(f'Worker PID {process.pid} is still finishing a task. It was preserved to avoid orphaning its process group.', file=sys.stderr)
                else:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
        if args.docker:
            print('Compose PG/Redis remain running to preserve data. Stop them with: docker compose stop', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Startup failed: {error}', file=sys.stderr)
        sys.exit(1)
