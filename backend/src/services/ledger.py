import base64
import contextlib
import json
import os
import tempfile
import time

try:
    import fcntl
except ImportError:  # 非 POSIX 环境退化为进程内串行；Compose 部署使用 Linux
    fcntl = None

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.settings import ARTIFACT_DIR
from src.models import (
    IdempotencyKey,
    LedgerAnchor,
    LedgerAnchorKey,
    LedgerEntry,
    LedgerPackage,
    LedgerState,
    User,
)
from src.services import ledger_package as pkg
from src.services.audit import record

EVENT_LABELS = {
    'dataset.ingest': '客流接入',
    'job.verified': '核验完成',
    'job.failed': '核验失败',
    'job.approved': '批准结论',
    'report.archived': '简报归档',
}
PACKAGE_CHUNK = 100
PENDING_LEASE_SECONDS = 120


# ---------------------------------------------------------------- 链式追加

def _state(db) -> LedgerState:
    state = db.get(LedgerState, 1)
    if state is None:
        state = LedgerState(id=1, last_seq=0, head_hash=pkg.GENESIS_HASH, updated_at=time.time())
        db.add(state)
        db.flush()
    return state


def lock_ledger(db) -> LedgerState:
    """串行化凭据追加：并发事务在这里排队，拿到锁后重新读取最新链尾。"""
    _state(db)
    return db.execute(
        select(LedgerState)
        .where(LedgerState.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def append(db, event, actor, subject, payload=None, dedupe_key=None):
    """在调用方的事务里追加一条凭据。

    约束：不 commit、不 rollback。调用方把业务写入和凭据写入放在同一事务提交，
    任何一方失败都会一起回滚，避免“业务已落定但凭据缺失”。

    dedupe_key 非空时，同一业务动作只写一条凭据；并发重复提交会撞到唯一约束，
    由调用方的完整性错误分支转成“已办理”回放。
    """
    state = lock_ledger(db)
    seq = state.last_seq + 1
    now = time.time()
    document = pkg.entry_document(seq, event, actor, subject, payload or {}, state.head_hash, now)
    db.add(
        LedgerEntry(
            seq=seq,
            event=event,
            actor=actor,
            subject=subject,
            payload=payload or {},
            payload_hash=document['payload_hash'],
            prev_hash=document['prev_hash'],
            entry_hash=document['entry_hash'],
            created_at=document['created_at'],
            dedupe_key=dedupe_key,
        )
    )
    state.last_seq = seq
    state.head_hash = document['entry_hash']
    state.updated_at = now
    db.flush()
    return document


def entries_through(db, cutoff):
    return list(db.scalars(select(LedgerEntry).where(LedgerEntry.seq <= cutoff).order_by(LedgerEntry.seq)))


def current_seq(db):
    return _state(db).last_seq


def entry_document(entry: LedgerEntry) -> dict:
    return {
        'seq': entry.seq,
        'event': entry.event,
        'actor': entry.actor,
        'subject': entry.subject,
        'payload': entry.payload,
        'payload_hash': entry.payload_hash,
        'prev_hash': entry.prev_hash,
        'entry_hash': entry.entry_hash,
        'created_at': entry.created_at,
    }


def present(entry: LedgerEntry) -> dict:
    return {**entry_document(entry), 'event_label': EVENT_LABELS.get(entry.event, entry.event)}


# ---------------------------------------------------------------- 可信锚点密钥

def _signing_key(db, create=False):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception:
        raise HTTPException(503, '服务器缺少签名组件，暂不能导出可信锚点')
    row = db.get(LedgerAnchorKey, 1)
    if row is None:
        if not create:
            return None
        key = Ed25519PrivateKey.generate()
        raw_private = key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        raw_public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        row = LedgerAnchorKey(
            id=1,
            public_key=base64.b64encode(raw_public).decode('ascii'),
            private_key=base64.b64encode(raw_private).decode('ascii'),
            created_at=time.time(),
        )
        db.add(row)
        db.flush()
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(row.private_key))
    return private, row.public_key


def ensure_anchor_key(db):
    """启动时生成并持久化锚点密钥，保证公钥指纹在首次出包前就稳定可登记。"""
    stored = _signing_key(db, create=True)
    db.commit()
    return {'public_key': stored[1], 'key_id': pkg.sha256_hex(base64.b64decode(stored[1]))[:16]}


def initialize(db):
    """启动时固定链的起点行，避免并发首写时争抢插入。"""
    _state(db)
    db.commit()


def all_packages(db):
    return list(db.scalars(select(LedgerPackage).order_by(LedgerPackage.cutoff_seq.desc())))


def public_key_info(db):
    stored = _signing_key(db, create=False)
    if not stored:
        return None
    _, public_key = stored
    return {'public_key': public_key, 'key_id': pkg.sha256_hex(base64.b64decode(public_key))[:16]}


# ------------------------------------------------------------------ 核验包

def package_filename(package_id, cutoff):
    return f'ledger-package-{package_id:04d}-to-{cutoff:04d}.json'


def package_path(package: LedgerPackage):
    return os.path.join(ARTIFACT_DIR, package.filename) if package.filename else None


def staging_path(path):
    return path + '.partial'


@contextlib.contextmanager
def package_lock(path):
    """同一截止序号的出包与续写串行化；进程崩溃时内核自动释放锁。"""
    if fcntl is None:
        yield
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.lock', 'a+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _staged_entries(path):
    """读取上次中断时已写下的凭据行；这是“同次内容”的实物依据。

    进程若在写单行途中被杀，末行可能残缺：丢弃残行并回写，续写从完整行之后接上。
    """
    staged = []
    if not os.path.exists(path):
        return staged
    with open(path, encoding='utf-8') as stream:
        lines = stream.readlines()
    good = []
    for line in lines:
        if not line.strip():
            continue
        try:
            staged.append(json.loads(line))
            good.append(line if line.endswith('\n') else line + '\n')
        except ValueError:
            break
    if len(good) != len(lines):
        with open(path, 'w', encoding='utf-8') as stream:
            stream.writelines(good)
            stream.flush()
            os.fsync(stream.fileno())
    return staged


def _append_staged(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(''.join(pkg.canonical_json(entry) + '\n' for entry in entries))
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.ledger-', suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def _write_package(db, package):
    """按截止序号重新读取凭据并原子写盘。

    同样的截止序号一定得到同样的内容：中断后重试是续写而不是另起一包。
    截止序号之后到达的凭据不进入本包，留给下一包。
    """
    if not package.filename:
        # 进程在创建记录与命名之间中断时，重试用同一规则补回文件名
        package.filename = package_filename(package.id, package.cutoff_seq)
        db.commit()
    path = package_path(package)
    partial = staging_path(path)
    with package_lock(path):
        package.status = 'building'
        db.commit()
        staged = _staged_entries(partial)
        cutoff = entries_through(db, package.cutoff_seq)
        cutoff_seqs = [entry.seq for entry in cutoff]
        # 中断前已落盘的凭据必须与本次一致，否则说明原库与中间文件对不上，宁可报错也不静默改动
        if [entry['seq'] for entry in staged] != cutoff_seqs[:len(staged)]:
            raise HTTPException(409, '核验包中间文件与凭据链不一致，请人工核查后再出包')
        package.cursor = staged[-1]['seq'] if staged else 0
        db.commit()
        for start in range(len(staged), len(cutoff), PACKAGE_CHUNK):
            chunk = [entry_document(entry) for entry in cutoff[start:start + PACKAGE_CHUNK]]
            _append_staged(partial, chunk)
            package.cursor = chunk[-1]['seq']
            db.commit()
        entries = _staged_entries(partial)
        document = pkg.build_package_document(package.id, package.cutoff_seq, package.created_at, entries)
        _atomic_write(path, pkg.package_text(document))
        if os.path.exists(partial):
            os.unlink(partial)
        package.entry_count = len(entries)
        package.head_hash = document['head_hash']
        package.package_hash = document['package_hash']
        package.cursor = document['cutoff_seq']
        package.status = 'complete'
        if package.closed_at is None:
            package.closed_at = time.time()
        db.commit()
    return package


def _needs_rebuild(package: LedgerPackage) -> bool:
    if package.status != 'complete':
        return True
    path = package_path(package)
    return bool(path) and not os.path.exists(path)


def anchor_valid(db, package: LedgerPackage) -> bool:
    """锚点是否仍与该包一致；文件被重建或核验包被重建后不能继续沿用旧锚点。"""
    row = _anchor_row(db, package.id)
    return bool(row) and row.package_hash == package.package_hash and row.head_hash == package.head_hash


def start_package(db, cutoff, user):
    state = _state(db)
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff < 1 or cutoff > state.last_seq:
        raise HTTPException(422, f'截至序号需在 1 到当前序号 {state.last_seq} 之间')
    existing = db.scalar(select(LedgerPackage).where(LedgerPackage.cutoff_seq == cutoff))
    if existing:
        if _needs_rebuild(existing):
            _write_package(db, existing)
        return existing, False
    package = LedgerPackage(
        cutoff_seq=cutoff,
        status='building',
        cursor=0,
        filename='',
        created_by=user.id,
        created_at=time.time(),
    )
    db.add(package)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(LedgerPackage).where(LedgerPackage.cutoff_seq == cutoff))
        if not existing:
            raise HTTPException(409, '核验包创建冲突，请重试')
        if _needs_rebuild(existing):
            _write_package(db, existing)
        return existing, False
    package.filename = package_filename(package.id, cutoff)
    db.commit()
    _write_package(db, package)
    record(db, user.email, 'ledger.package', {'package': package.id, 'cutoff': cutoff, 'entries': package.entry_count})
    db.commit()
    return package, True


def resume_pending(db):
    """启动时接续上次中断的出包，保证同一截止序号得到同一份内容。

    单个包续写失败只记录日志，不阻断服务启动；负责人可再次点击同一截止序号重试。
    """
    import logging

    pending = list(db.scalars(select(LedgerPackage).where(LedgerPackage.status != 'complete')))
    resumed = 0
    for package in pending:
        try:
            _write_package(db, package)
            resumed += 1
        except Exception:
            db.rollback()
            logging.exception('Resume ledger package %s failed', package.id)
    return resumed


def ensure_anchor(db, package: LedgerPackage, user=None):
    """为完整包生成（或复取）独立签名的可信锚点。重试不产生第二份锚点。"""
    existing = _anchor_row(db, package.id)
    if existing and anchor_valid(db, package):
        return existing
    if package.status != 'complete':
        raise HTTPException(409, '核验包尚未完成，不能另存可信锚点')
    if existing:
        # 包被重建导致摘要变化时，旧锚点作废；同一截止序号只保留一份有效锚点
        db.delete(existing)
        db.flush()
    private, public_key = _signing_key(db, create=True)
    anchor = pkg.anchor_document(
        package.id,
        package.package_hash,
        package.cutoff_seq,
        package.entry_count,
        package.head_hash,
        package.closed_at or time.time(),
        public_key,
        '',
    )
    anchor['signature'] = pkg.sign_anchor(anchor, private)
    row = LedgerAnchor(
        package_id=package.id,
        package_hash=anchor['package_hash'],
        cutoff_seq=anchor['cutoff_seq'],
        entry_count=anchor['entry_count'],
        head_hash=anchor['head_hash'],
        key_id=anchor['key_id'],
        public_key=anchor['public_key'],
        signature=anchor['signature'],
        filename=f'ledger-anchor-{package.id:04d}-to-{package.cutoff_seq:04d}.json',
        created_by=user.id if user else package.created_by,
        created_at=anchor['created_at'],
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(LedgerAnchor).where(LedgerAnchor.package_id == package.id))
        if existing:
            return existing
        raise
    _atomic_write(os.path.join(ARTIFACT_DIR, row.filename), pkg.anchor_text(anchor))
    record(db, user.email if user else 'system', 'ledger.anchor', {'package': package.id, 'cutoff': package.cutoff_seq})
    db.commit()
    return row


def anchor_document(row: LedgerAnchor) -> dict:
    return pkg.anchor_document(
        row.package_id,
        row.package_hash,
        row.cutoff_seq,
        row.entry_count,
        row.head_hash,
        row.created_at,
        row.public_key,
        row.signature,
    )


def _anchor_row(db, package_id):
    return db.scalar(select(LedgerAnchor).where(LedgerAnchor.package_id == package_id))


def package_status(db, package: LedgerPackage) -> dict:
    anchor = _anchor_row(db, package.id)
    if anchor and not anchor_valid(db, package):
        anchor = None
    path = package_path(package)
    ready = bool(path) and os.path.exists(path) and package.status == 'complete'
    creator = db.get(User, package.created_by) if package.created_by else None
    info = {
        'id': package.id,
        'cutoff_seq': package.cutoff_seq,
        'status': package.status,
        'entry_count': package.entry_count,
        'head_hash': package.head_hash,
        'package_hash': package.package_hash,
        'cursor': package.cursor,
        'created_by': creator.name if creator else '系统',
        'created_at': package.created_at,
        'closed_at': package.closed_at,
        'filename': package.filename,
        'anchored': anchor is not None,
        'anchor_filename': anchor.filename if anchor else '',
        'key_id': anchor.key_id if anchor else '',
        'download_ready': ready,
        'verification': None,
    }
    if package.status != 'complete':
        info['verification'] = {
            'status': 'damaged',
            'label': pkg.STATUS_LABELS['damaged'],
            'summary': f'核验包尚未完成（已推进到第 {package.cursor} 条），当前文件不可作为凭据。',
            'problems': [],
        }
    elif not ready:
        info['verification'] = {
            'status': 'damaged',
            'label': pkg.STATUS_LABELS['damaged'],
            'summary': '核验包文件已丢失；请对同一截止序号重新出包，内容与首次一致。',
            'problems': [],
        }
    else:
        info['verification'] = _check_files(path, anchor)
    return info


def _check_files(path, anchor):
    try:
        with open(path, encoding='utf-8') as stream:
            document = json.load(stream)
    except (OSError, ValueError) as error:
        return {
            'status': 'damaged',
            'label': pkg.STATUS_LABELS['damaged'],
            'summary': f'无法读取核验包：{error}',
            'problems': [],
        }
    report = pkg.verify_package_document(document, anchor_document(anchor) if anchor else None)
    return {
        'status': report['status'],
        'label': report['label'],
        'summary': report['summary'],
        'problems': report['problems'][:8],
    }


def verify_package_files(db, package: LedgerPackage) -> dict:
    path = package_path(package)
    if package.status != 'complete' or not path or not os.path.exists(path):
        raise HTTPException(409, '核验包尚未完成，暂无核验结果')
    anchor = _anchor_row(db, package.id)
    try:
        with open(path, encoding='utf-8') as stream:
            document = json.load(stream)
    except (OSError, ValueError) as error:
        raise HTTPException(409, f'无法读取核验包：{error}')
    return pkg.verify_package_document(document, anchor_document(anchor) if anchor else None)


def ledger_overview(db):
    state = _state(db)
    counts = dict(db.execute(select(LedgerEntry.event, func.count()).group_by(LedgerEntry.event)).all())
    last = db.scalar(select(LedgerEntry).where(LedgerEntry.seq == state.last_seq))
    return {
        'last_seq': state.last_seq,
        'head_hash': state.head_hash,
        'updated_at': state.updated_at,
        'last_event': present(last) if last else None,
        'events': {EVENT_LABELS.get(k, k): v for k, v in counts.items()},
        'event_codes': counts,
        'key': public_key_info(db),
    }


# ------------------------------------------------------------------ 幂等

def claim(db, key, scope):
    """先占用幂等键。

    返回 ('proceed', None) 表示本次是新请求，需要真正办理；
    返回 ('replay', response) 表示同一请求已经办过，直接回放首次结果；
    返回 ('pending', None) 表示另一个同键请求正在办理，调用方应稍后回放。
    """
    if not key:
        return 'proceed', None
    key = key.strip()
    if not 1 <= len(key) <= 120:
        raise HTTPException(422, '幂等键长度需在 1 到 120 之间')
    now = time.time()
    found = db.get(IdempotencyKey, key)
    if found:
        if found.scope != scope:
            raise HTTPException(409, '该幂等键已用于其他操作')
        if found.state == 'complete':
            return 'replay', found.response
        if now - found.created_at < PENDING_LEASE_SECONDS:
            # 并发同键请求：稍等首个请求落定后回放，而不是重复办理
            return 'pending', None
        found.created_at = now
        found.response = {}
        db.commit()
        return 'proceed', None
    db.add(IdempotencyKey(key=key, scope=scope, state='pending', response={}, created_at=now))
    try:
        db.commit()
        return 'proceed', None
    except IntegrityError:
        db.rollback()
        found = db.get(IdempotencyKey, key)
        if found and found.scope == scope and found.state == 'complete':
            return 'replay', found.response
        return 'pending', None


def finish(db, key, response):
    """与业务写入同一事务落定首次结果；不单独提交。"""
    if not key:
        return
    found = db.get(IdempotencyKey, key.strip())
    if found:
        found.state = 'complete'
        found.response = response
        db.flush()
