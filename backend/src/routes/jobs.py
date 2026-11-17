from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Job, LedgerEntry
from src.schemas.job import JobInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import jobs, ledger
from src.services import ledger_package
from src.services.audit import record
from src.utils import idempotency
from src.utils.serialization import job_dict
router=APIRouter(prefix='/jobs')
@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [job_dict(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(100))]
@router.post('',status_code=201)
def create(data:JobInput,db=Depends(get_db),u=Depends(can_write),idem:str|None=Header(default=None,alias='Idempotency-Key')):
    def handler():
        j=jobs.create(db,data,u); record(db,u.email,'job.create',{'id':j.id,'params':j.params})
        db.flush()
        return job_dict(j)
    payload,replayed=idempotency.run(db,idem,'job.create',handler)
    if not replayed: jobs.enqueue(payload['id'])
    return {**payload,'replayed':replayed}
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    j=db.get(Job,id)
    if not j: raise HTTPException(404,'分析任务不存在')
    return job_dict(j)
@router.post('/{id}/approve')
def approve(id:int,db=Depends(get_db),u=Depends(admin_only),idem:str|None=Header(default=None,alias='Idempotency-Key')):
    def handler():
        j=db.get(Job,id)
        if not j: raise HTTPException(404,'分析任务不存在')
        if j.status!='completed':raise HTTPException(409,'仅已完成结果可以批准')
        existing=db.scalar(select(LedgerEntry).where(LedgerEntry.dedupe_key==f'job.approved:{j.id}'))
        if not j.approved:
            j.approved=True
            entry=existing or ledger.append(db,'job.approved',u.email,f'job:{j.id}',{'id':j.id,'name':j.name,'dataset_id':j.dataset_id,'params':j.params,'result_checksum':ledger_package.sha256_hex(ledger_package.canonical_json(j.result)),'verification_seq':_verified_seq(db,j.id)},dedupe_key=f'job.approved:{j.id}')
            entry_seq=entry.seq if isinstance(entry,LedgerEntry) else entry['seq']
            record(db,u.email,'job.approve',{'id':id,'ledger_seq':entry_seq})
        else:
            entry_seq=existing.seq if existing else None
        db.flush()
        return {**job_dict(j),'ledger_seq':entry_seq}
    payload,replayed=idempotency.run(db,idem,f'job.approve:{id}',handler)
    return {**payload,'replayed':replayed}
def _verified_seq(db,job_id):
    row=db.scalar(select(LedgerEntry).where(LedgerEntry.dedupe_key==f'job.verified:{job_id}'))
    return row.seq if row else None
