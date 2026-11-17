import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select

from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.middlewares.auth import admin_only, current_user
from src.models import LedgerAnchor, LedgerEntry, LedgerPackage
from src.schemas.ledger import OfflineCheck, PackageInput
from src.services import ledger, ledger_package

router = APIRouter(prefix='/ledger')


def _package(db, package_id: int) -> LedgerPackage:
    package = db.get(LedgerPackage, package_id)
    if not package:
        raise HTTPException(404, '核验包不存在')
    return package


@router.get('')
def overview(db=Depends(get_db), user=Depends(current_user)):
    return ledger.ledger_overview(db)


@router.get('/entries')
def entries(
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(get_db),
    user=Depends(current_user),
):
    rows = db.scalars(
        select(LedgerEntry).where(LedgerEntry.seq > after).order_by(LedgerEntry.seq).limit(limit)
    )
    items = [ledger.present(row) for row in rows]
    return {
        'items': items,
        'last_seq': ledger.current_seq(db),
        'next_after': items[-1]['seq'] if items else after,
    }


@router.get('/packages')
def packages(db=Depends(get_db), user=Depends(current_user)):
    return [ledger.package_status(db, row) for row in ledger.all_packages(db)]


@router.post('/packages', status_code=201)
def create_package(data: PackageInput, db=Depends(get_db), user=Depends(admin_only)):
    package, created = ledger.start_package(db, data.cutoff_seq, user)
    info = ledger.package_status(db, package)
    return {**info, 'reused': not created}


@router.get('/packages/{package_id}')
def package_detail(package_id: int, db=Depends(get_db), user=Depends(current_user)):
    return ledger.package_status(db, _package(db, package_id))


@router.post('/packages/{package_id}/anchor', status_code=201)
def create_anchor(package_id: int, db=Depends(get_db), user=Depends(admin_only)):
    package = _package(db, package_id)
    reused = ledger.anchor_valid(db, package)
    anchor = ledger.ensure_anchor(db, package, user)
    return {
        'package_id': package.id,
        'anchor': ledger.anchor_document(anchor),
        'filename': anchor.filename,
        'key_id': anchor.key_id,
        'reused': reused,
        'verification': ledger.package_status(db, package)['verification'],
    }


@router.get('/packages/{package_id}/download')
def download_package(package_id: int, db=Depends(get_db), user=Depends(current_user)):
    package = _package(db, package_id)
    path = ledger.package_path(package)
    if package.status != 'complete' or not path or not os.path.exists(path):
        raise HTTPException(409, '核验包尚未完成，暂不能下载')
    return FileResponse(path, filename=package.filename, media_type='application/json')


@router.get('/packages/{package_id}/anchor/download')
def download_anchor(package_id: int, db=Depends(get_db), user=Depends(current_user)):
    package = _package(db, package_id)
    anchor = db.scalar(select(LedgerAnchor).where(LedgerAnchor.package_id == package.id))
    if not anchor:
        raise HTTPException(409, '该核验包尚未另存可信锚点')
    path = os.path.join(ARTIFACT_DIR, anchor.filename)
    if not os.path.exists(path):
        raise HTTPException(404, '锚点文件缺失，请重新另存可信锚点')
    return FileResponse(path, filename=anchor.filename, media_type='application/json')


@router.get('/packages/{package_id}/verify')
def verify_package(package_id: int, db=Depends(get_db), user=Depends(current_user)):
    """服务端复核：只读取核验包与锚点文件，不重新读取业务库。"""
    return ledger.verify_package_files(db, _package(db, package_id))


@router.post('/verify')
def verify_upload(data: OfflineCheck, user=Depends(current_user)):
    """接受上传的核验包/锚点内容做同样的离线核验；不查业务库。"""
    return ledger_package.verify_package_document(data.package, data.anchor, data.expect_key_id)


@router.get('/anchor-key')
def anchor_key(db=Depends(get_db), user=Depends(current_user)):
    info = ledger.public_key_info(db)
    if not info:
        raise HTTPException(404, '尚未生成锚点公钥；首次另存可信锚点时会自动生成')
    return info
