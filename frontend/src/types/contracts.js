/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{seq:number,event:string,event_label:string,actor:string,subject:string,payload:Object,payload_hash:string,prev_hash:string,entry_hash:string,created_at:number}} LedgerEntry */
/** @typedef {{id:number,cutoff_seq:number,status:string,entry_count:number,head_hash:string,package_hash:string,anchored:boolean,download_ready:boolean,verification:{status:'complete'|'damaged'|'missing_anchor',label:string,summary:string,problems:Array}} LedgerPackage */
export {};
