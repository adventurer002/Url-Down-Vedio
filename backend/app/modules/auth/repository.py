from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from app.modules.auth.models import User


@dataclass(frozen=True)
class UserRecord:
    id: UUID
    nickname: str
    is_subscribed: bool
    role: str
    status: str


def record(user):
    return UserRecord(**{key: getattr(user, key) for key in UserRecord.__dataclass_fields__})


class UserRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def get(self, user_id: UUID):
        with self.sessions() as session:
            user = session.get(User, user_id)
            return record(user) if user else None

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
