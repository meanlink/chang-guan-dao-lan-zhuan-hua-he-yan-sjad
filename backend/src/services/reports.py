import time, os, csv, io, hashlib, json
from sqlalchemy import select
from fastapi import HTTPException
from src.models import Job, Report, Delivery, LedgerEntry
from src.config.settings import ARTIFACT_DIR
from src.services import ledger
def create(db, data):
    j=db.get(Job,data.job_id)
    if not j or j.status!='completed' or not j.approved:
        raise HTTPException(409,'请先选择已完成且获批准的分析结果')
    r=Report(name=data.name, job_id=j.id, destination=data.destination, interval_seconds=data.interval_seconds, next_run=time.time()+data.delay_seconds, enabled=True)
    db.add(r); db.flush()
    return r
def deliver(db, report):
    j=db.get(Job,report.job_id)
    if not j or not j.approved: return
    out=io.StringIO()
    out.write('﻿')
    table=j.result.get('table',{})
    cols=table.get('columns',[])
    writer=csv.writer(out)
    def safe(v):
        if isinstance(v,(int,float)) and not isinstance(v,bool): return v
        text=str(v)
        return "'"+text if text.startswith(('=','+','-','@','\t','\r')) else text
    writer.writerow([safe(c['label']) for c in cols])
    for row in table.get('rows',[]): writer.writerow([safe(row.get(c['key'],'')) for c in cols])
    data=out.getvalue().encode('utf-8')
    delivery=Delivery(report_id=report.id, created_at=time.time(), checksum=hashlib.sha256(data).hexdigest(), filename='', destination=report.destination)
    db.add(delivery); db.flush()
    delivery.filename=f'report-{report.id}-delivery-{delivery.id}.csv'
    os.makedirs(ARTIFACT_DIR,exist_ok=True)
    csv_path=os.path.join(ARTIFACT_DIR,delivery.filename)
    json_path=csv_path+'.json'
    with open(csv_path,'wb') as f:f.write(data)
    with open(json_path,'w') as f: json.dump({'job':j.name,'params':j.params,'result':j.result,'checksum':delivery.checksum},f,ensure_ascii=False,allow_nan=False)
    try:
        # 归档凭据与归档记录同一事务落定；提交失败一并回滚并清理落盘文件
        already=db.scalar(select(LedgerEntry).where(LedgerEntry.dedupe_key==f'report.archived:{report.id}:{delivery.id}'))
        if already is None:
            ledger.append(db,'report.archived','worker',f'report:{report.id}',{'report_id':report.id,'job_id':j.id,'delivery_id':delivery.id,'destination':report.destination,'filename':delivery.filename,'checksum':delivery.checksum,'verification_seq':_verified_seq(db,j.id)},dedupe_key=f'report.archived:{report.id}:{delivery.id}')
        report.next_run=time.time()+report.interval_seconds
        db.commit()
    except Exception:
        db.rollback()
        for path in (csv_path,json_path):
            if os.path.exists(path): os.unlink(path)
        raise
    return delivery
def _verified_seq(db,job_id):
    row=db.scalar(select(LedgerEntry).where(LedgerEntry.dedupe_key==f'job.verified:{job_id}'))
    return row.seq if row else None
