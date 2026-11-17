from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.models import Dataset
from src.schemas.dataset import DatasetInput
from src.middlewares.auth import current_user, can_write
from src.services import datasets, ledger
from src.services.audit import record
from src.utils import idempotency
from src.utils.serialization import dataset_dict
router=APIRouter(prefix='/datasets')
@router.get('')
def listing(db=Depends(get_db),u=Depends(current_user)):
    return [dataset_dict(d) for d in db.scalars(select(Dataset).order_by(Dataset.id.desc()))]
@router.post('',status_code=201)
def create(data:DatasetInput,db=Depends(get_db),u=Depends(can_write),idem:str|None=Header(default=None,alias='Idempotency-Key')):
    def handler():
        d=datasets.create(db,data)
        ledger.append(db,'dataset.ingest',u.email,f'dataset:{d.id}',{'id':d.id,'name':d.name,'rows':len(d.records),'checksum':d.checksum,'source':d.source},dedupe_key=f'dataset.ingest:{d.id}')
        record(db,u.email,'dataset.create',{'id':d.id,'rows':len(d.records)})
        db.flush()
        return dataset_dict(d)
    payload,replayed=idempotency.run(db,idem,'dataset.create',handler)
    return {**payload,'replayed':replayed}
@router.get('/{id}')
def detail(id:int,db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Dataset,id)
    if not d:raise HTTPException(404,'数据批次不存在')
    return {**dataset_dict(d),'records':d.records}
