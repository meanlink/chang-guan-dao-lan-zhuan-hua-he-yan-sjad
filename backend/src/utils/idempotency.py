"""幂等办理：同一请求键只办理一次，重试回放首次结果。"""

import time

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.models import IdempotencyKey
from src.services import ledger

WAIT_SECONDS = 6.0


def _replay_when_ready(db, key, scope):
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        db.rollback()
        found = db.get(IdempotencyKey, key)
        if found and found.scope == scope and found.state == 'complete':
            return found.response
        time.sleep(0.25)
    return None


def run(db, key, scope, handler):
    """返回 (响应, 是否回放)。

    handler 负责业务写入（含凭据）并返回可序列化响应。本函数占用键、
    提交、异常回滚，并在业务失败时释放键，使调用方可以安全重试；
    业务失败绝不留下半个凭据或半个幂等结果。
    """
    mode, response = ledger.claim(db, key, scope)
    if mode == 'replay':
        return response, True
    if mode == 'pending':
        replayed = _replay_when_ready(db, key.strip(), scope)
        if replayed is not None:
            return replayed, True
        raise HTTPException(409, '相同请求正在处理，请稍后重试')
    try:
        result = handler()
        ledger.finish(db, key, result)
        db.commit()
        return result, False
    except IntegrityError:
        db.rollback()
        if key:
            stored = db.get(IdempotencyKey, key.strip())
            if stored and stored.state == 'pending':
                db.delete(stored)
                db.commit()
        raise HTTPException(409, '数据与既有记录冲突，请核对后重试')
    except Exception:
        db.rollback()
        if key:
            stored = db.get(IdempotencyKey, key.strip())
            if stored and stored.state == 'pending':
                db.delete(stored)
                db.commit()
        raise
