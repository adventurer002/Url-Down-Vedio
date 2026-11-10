from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text

from app.core.exceptions import AppException

from app.modules.auth.models import User


@dataclass(frozen=True)
class UserRecord:
    id: UUID
    nickname: str
    is_subscribed: bool
    role: str
    status: str
    auth_version: int = 0


def public_user(user):
    data = asdict(user)
    data.pop("auth_version", None)
    return data


def record(user):
    return UserRecord(**{key: getattr(user, key) for key in UserRecord.__dataclass_fields__})


class UserRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def get(self, user_id: UUID):
        with self.sessions() as session:
            user = session.get(User, user_id)
            return record(user) if user else None

    @staticmethod
    def admin_record(user):
        return {**{name: getattr(user, name) for name in (
            "id", "nickname", "is_subscribed", "role", "status", "created_at", "last_login_at")},
            "status_version": user.auth_version}

    def require_admin(self, session, actor_id, version):
        user = session.get(User, actor_id, populate_existing=True)
        if user is None or user.status != "active" or user.auth_version != version:
            raise AppException("INVALID_TOKEN", "登录凭证已失效，请重新登录", 401)
        if user.role != "admin":
            raise AppException("FORBIDDEN", "此操作仅限管理员", 403)
        return record(user)

    def change_status(self, session, user_id, status, expected_status, expected_version):
        user = session.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
        if user is None:
            raise AppException("USER_NOT_FOUND", "用户不存在", 404)
        if user.role != "user":
            raise AppException("ADMIN_ACCOUNT_PROTECTED", "不能通过网页变更管理员账号状态", 403)
        if user.status != expected_status or user.auth_version != expected_version:
            raise AppException("USER_STATUS_CONFLICT", "账号状态已变化，请刷新后重试", 409)
        before = {"status": user.status, "status_version": user.auth_version}
        if user.status != status:
            user.status = status
            user.auth_version += 1
        session.flush()
        return before, {"status": user.status, "status_version": user.auth_version}

    def admin_detail(self, session, user_id):
        user = session.get(User, user_id, populate_existing=True)
        if user is None:
            raise AppException("USER_NOT_FOUND", "用户不存在", 404)
        return self.admin_record(user)

    def display_names(self, session, user_ids):
        return dict(session.execute(select(User.id, User.nickname).where(User.id.in_(user_ids))).all())

    def admin_list(self, session, limit, offset, q, status, subscribed, member, member_ids):
        conditions = []
        if q and (q := q.strip()):
            search = User.nickname.icontains(q, autoescape=True)
            try:
                search = or_(search, User.id == UUID(q))
            except ValueError:
                pass
            conditions.append(search)
        if status is not None:
            conditions.append(User.status == status)
        if subscribed is not None:
            conditions.append(User.is_subscribed == subscribed)
        if member is not None:
            condition = User.id.in_(member_ids)
            conditions.append(condition if member else ~condition)
        total = session.scalar(select(func.count()).select_from(User).where(*conditions))
        rows = session.scalars(select(User).where(*conditions)
            .order_by(User.created_at.desc(), User.id.desc()).limit(limit).offset(offset))
        return [self.admin_record(user) for user in rows], total

    def admin_statistics(self, session):
        total = session.scalar(select(func.count()).select_from(User))
        active = session.scalar(select(func.count()).select_from(User).where(User.status == "active"))
        subscribed = session.scalar(select(func.count()).select_from(User).where(User.is_subscribed.is_(True)))
        return {"total": total, "active": active, "disabled": total - active, "subscribed": subscribed}

    def lock_admin_initialization(self, session):
        # Serialize even when the users/admin set is empty; locking a selected user
        # alone would allow two distinct accounts to become the first admin.
        if session.bind.dialect.name == "postgresql":
            session.execute(select(func.pg_advisory_xact_lock(724019872001)))
        elif session.bind.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            raise AppException("ADMIN_DATABASE_UNSUPPORTED", "当前数据库不支持安全初始化", 503)

    def initialize_admin(self, session, user_id):
        if session.scalar(select(User.id).where(User.role == "admin").limit(1)) is not None:
            raise AppException("ADMIN_ALREADY_INITIALIZED", "已有管理员，不能再次初始化", 409)
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise AppException("USER_NOT_FOUND", "用户不存在", 404)
        if user.role != "user" or user.status != "active" or user.last_login_at is None:
            raise AppException("ADMIN_USER_INELIGIBLE", "只能初始化已登录绑定的活跃普通账号", 409)
        user.role = "admin"
        session.flush()

    def billing_identity(self, session, user_id):
        """Read-only identity contract participating in an allowance transaction."""
        user = session.get(User, user_id, populate_existing=True)
        if user is None:
            return None
        return {"openid": user.wechat_openid, "subscribed": user.is_subscribed,
                "active": user.status == "active", "bound": user.last_login_at is not None}

    def subscription(self, openid: str, subscribed: bool, login: bool = False):
        with self.sessions.begin() as session:
            # Concurrent webhook deliveries must converge on one unique OpenID.
            if session.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            session.execute(insert(User).values(
                id=uuid4(), wechat_openid=openid, nickname="微信用户_" + openid[-4:],
                is_subscribed=subscribed, role="user", status="active"
            ).on_conflict_do_nothing(index_elements=[User.wechat_openid]))
            user = session.scalar(select(User).where(User.wechat_openid == openid).with_for_update())
            user.is_subscribed = subscribed
            if login:
                user.last_login_at = datetime.now(timezone.utc)
            session.flush()
            return record(user)
