import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report, LedgerEntry
from src.services.business import analyze
from src.services.reports import deliver
from src.services import ledger, ledger_package
from src.services.audit import record
logging.basicConfig(level=logging.INFO)
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        j.status='running'; db.commit()
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            j.result=result;j.status='completed';j.finished_at=time.time()
            # 核验结论与凭据同一事务提交：任何一方失败都整体回滚，任务回到排队
            already=db.scalar(select(LedgerEntry).where(LedgerEntry.dedupe_key==f'job.verified:{j.id}'))
            if already is None:
                ledger.append(db,'job.verified','worker',f'job:{j.id}',{'id':j.id,'dataset_id':j.dataset_id,'dataset_checksum':d.checksum,'params':j.params,'result_checksum':ledger_package.sha256_hex(ledger_package.canonical_json(result))},dedupe_key=f'job.verified:{j.id}')
            record(db,'worker','job.completed',{'id':j.id})
            db.commit()
        except Exception as e:
            logging.exception('Analysis failed')
            db.rollback()
            j=db.get(Job,job_id)
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
            db.commit()
def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            try:
                d=deliver(db,r)
            except Exception:
                logging.exception('Delivery failed; will retry next tick')
                db.rollback()
                continue
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
            db.commit()
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
