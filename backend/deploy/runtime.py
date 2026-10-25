"""Run as videohub, with its release venv, never as root."""
import os
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values

config = dotenv_values('/etc/videohub/backend.env')
os.environ.update({key: value for key, value in config.items() if value is not None})


def preflight():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from redis import Redis
    from app.core.config import settings

    if settings.APP_ENV != 'production' or not settings.AUTH_REQUIRED or not settings.ALLOWANCES_ENABLED:
        raise ValueError('Production environment and authentication/allowances must be enabled')
    if len(settings.JWT_SECRET_KEY) < 32 or settings.JWT_SECRET_KEY.startswith(('REPLACE', 'dev-secret')):
        raise ValueError('JWT_SECRET_KEY must be configured')
    if settings.WECHAT_MP_MODE == 'mock':
        raise ValueError('Production mock login is forbidden')
    if settings.storage_path != Path('/opt/videohub/storage'):
        raise ValueError('STORAGE_LOCAL_PATH must be /opt/videohub/storage')
    if make_url(settings.DATABASE_URL).database != 'videohub_prod':
        raise ValueError('Deployment backup expects database videohub_prod')
    engine = create_engine(settings.DATABASE_URL, connect_args={'connect_timeout': 5})
    try:
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
    finally:
        engine.dispose()
    for url in (settings.REDIS_URL, settings.CELERY_BROKER_URL, settings.CELERY_RESULT_BACKEND):
        with Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5) as client:
            client.ping()
    pending = []
    for name in ('ASR_API_KEY', 'LLM_API_KEY', 'ASR_MODEL', 'LLM_MODEL', 'WECHAT_MP_TOKEN'):
        value = getattr(settings, name)
        value = value.get_secret_value() if hasattr(value, 'get_secret_value') else value
        if not value or value.startswith('REPLACE'):
            pending.append(name)
    if pending:
        print('Business configuration pending: ' + ', '.join(pending), flush=True)
    print('Core production configuration, database and Redis verified', flush=True)


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['preflight']:
            preflight()
        elif sys.argv[1:] == ['migrate']:
            subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], check=True, timeout=120)
        else:
            raise ValueError('Expected preflight or migrate')
    except Exception as exc:
        # Connection errors may contain credentials. Only disclose the type here.
        print(f'Runtime operation failed: {type(exc).__name__}; inspect private server configuration', file=sys.stderr)
        raise SystemExit(1)
