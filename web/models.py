"""SQLAlchemy models — user/auth/backtest state only. Market data stays in parquet."""
import uuid
from datetime import datetime, timedelta, date
from sqlalchemy import Column, String, DateTime, Integer, Float, Boolean, Text, ForeignKey
from sqlalchemy.orm import relationship
from web.db import Base

def _utcnow():
    return datetime.utcnow()

def _gen_id():
    return uuid.uuid4().hex[:12]


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=_gen_id)
    username = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="member")  # admin | member
    is_active = Column(Boolean, default=True)  # admin can disable
    expires_at = Column(DateTime, nullable=True)  # account expiry
    invite_code = Column(String, nullable=True)  # invite code used to register
    created_at = Column(DateTime, default=_utcnow)
    last_login = Column(DateTime)

    holdings = relationship("UserHoldings", back_populates="user", uselist=False,
                           cascade="all, delete-orphan")
    backtest_jobs = relationship("BacktestJob", back_populates="user",
                                cascade="all, delete-orphan")


class InviteCode(Base):
    __tablename__ = "invite_codes"

    code = Column(String, primary_key=True)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    used_by = Column(String, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)

    @property
    def is_valid(self):
        return self.used_by is None and datetime.utcnow() < self.expires_at


class UserHoldings(Base):
    __tablename__ = "user_holdings"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    data_json = Column(Text, nullable=False)  # JSON array of {code, name, cost, shares, buy_date}
    updated_at = Column(DateTime, default=_utcnow)

    user = relationship("User", back_populates="holdings")


class BacktestJob(Base):
    __tablename__ = "backtest_jobs"

    id = Column(String, primary_key=True, default=_gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    status = Column(String, default="queued")  # queued | running | done | error
    config_json = Column(Text, nullable=False)  # BacktestConfig.to_dict()
    config_b_json = Column(Text, nullable=True)  # for A/B compare
    progress = Column(Integer, default=0)
    result_json = Column(Text, nullable=True)   # metrics + nav points
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="backtest_jobs")
