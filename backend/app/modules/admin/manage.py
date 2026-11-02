"""Initialize the first administrator from an existing bound account. No HTTP equivalent."""
import argparse
import asyncio
import json
from uuid import UUID

from app.core.exceptions import AppException
from app.infrastructure.database.session import SessionLocal
from app.modules.admin.service import AdminService
from app.modules.tasks.service import database_call


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--user-id", type=UUID, required=True)
    bootstrap.add_argument("--actor", required=True)
    bootstrap.add_argument("--reason", required=True)
    bootstrap.add_argument("--key", required=True)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(database_call(AdminService(SessionLocal).bootstrap,
            args.user_id, args.actor, args.reason, args.key))
    except AppException as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({"error": {"code": "ADMIN_OPERATION_FAILED", "message": "初始化失败，请检查本机服务配置"}}, ensure_ascii=False))
        return 1
    print(json.dumps({"data": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
