from sqlalchemy import String, JSON, Double, Integer, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.services.ledger_package import GENESIS_HASH


class LedgerState(Base):
    """单行表，保存凭据链的当前序号和链末端哈希。"""

    __tablename__ = 'ledger_state'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), default=GENESIS_HASH)
    updated_at: Mapped[float] = mapped_column(Double, default=0.0)


class LedgerEntry(Base):
    __tablename__ = 'ledger_entries'
    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    event: Mapped[str] = mapped_column(String(60), index=True)
    actor: Mapped[str] = mapped_column(String(150))
    subject: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Double)
    # 同一业务动作只允许一条凭据；重试或并发重复提交由唯一约束兜底
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)


class LedgerAnchorKey(Base):
    __tablename__ = 'ledger_anchor_keys'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    public_key: Mapped[str] = mapped_column(Text)
    private_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Double)


class LedgerPackage(Base):
    __tablename__ = 'ledger_packages'
    id: Mapped[int] = mapped_column(primary_key=True)
    # 一个截止序号只对应一个核验包：重试与并发都复用同一包，不重复出包
    cutoff_seq: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(String(20), default='building')
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), default=GENESIS_HASH)
    package_hash: Mapped[str] = mapped_column(String(64), default='')
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    filename: Mapped[str] = mapped_column(String(200), default='')
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
    closed_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class LedgerAnchor(Base):
    __tablename__ = 'ledger_anchors'
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey('ledger_packages.id'), unique=True)
    package_hash: Mapped[str] = mapped_column(String(64))
    cutoff_seq: Mapped[int] = mapped_column(Integer)
    entry_count: Mapped[int] = mapped_column(Integer)
    head_hash: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(32))
    public_key: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class IdempotencyKey(Base):
    __tablename__ = 'idempotency_keys'
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    scope: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(20), default='pending')
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Double)
