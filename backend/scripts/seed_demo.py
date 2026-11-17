import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, LedgerEntry
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import ledger, ledger_package
from src.utils.security import hash_password
from src.utils.serialization import checksum
def _has(db, key):
    return db.scalar(select(LedgerEntry).where(LedgerEntry.dedupe_key==key)) is not None
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not _has(db,f'dataset.ingest:{d.id}'):
            ledger.append(db,'dataset.ingest','seed',f'dataset:{d.id}',{'id':d.id,'name':d.name,'rows':len(d.records),'checksum':d.checksum,'source':d.source},dedupe_key=f'dataset.ingest:{d.id}')
        db.flush()
        admin=db.scalar(select(User).where(User.role=='admin'))
        j=db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name']))
        if not j:
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
        if not _has(db,f'job.verified:{j.id}'):
            ledger.append(db,'job.verified','worker',f'job:{j.id}',{'id':j.id,'dataset_id':d.id,'dataset_checksum':d.checksum,'params':j.params,'result_checksum':ledger_package.sha256_hex(ledger_package.canonical_json(j.result))},dedupe_key=f'job.verified:{j.id}')
        if not _has(db,f'job.approved:{j.id}'):
            verified=db.scalar(select(LedgerEntry).where(LedgerEntry.dedupe_key==f'job.verified:{j.id}'))
            ledger.append(db,'job.approved',admin.email,f'job:{j.id}',{'id':j.id,'name':j.name,'dataset_id':d.id,'params':j.params,'result_checksum':ledger_package.sha256_hex(ledger_package.canonical_json(j.result)),'verification_seq':verified.seq if verified else None},dedupe_key=f'job.approved:{j.id}')
        db.commit()
        r=db.scalar(select(Report).where(Report.name=='每日业务复核简报'))
        if not r:
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush()
            deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,LedgerEntry]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
