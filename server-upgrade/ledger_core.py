"""COWMATA ledger: canonical CSV schema, validation and durable local store."""
from __future__ import annotations
import csv, hashlib, io, json, os, re, sqlite3, tempfile, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ZONE = timezone(timedelta(hours=8))
BUSINESS = ["序号","牛号","预产期","设备号","佩戴开始","佩戴结束","产犊开始","产犊结束","监测目的","九轴","脉搏","温度","尾环佩戴人","事件标注人","样本评价"]
EXTRA = ["记录日期","牧场","数据分类","现场标记","归类目录","核对提示","来源","原始单元格","时间说明"]
META = ["记录ID","版本","修改时间","已删除"]
FIELDS = BUSINESS + EXTRA + META
CATEGORIES = {"healthy":"正常","estrus":"发情","pregnancy":"怀孕","pregnancy_early":"怀孕/孕早期","pregnancy_mid":"怀孕/孕中期","pregnancy_late":"怀孕/孕晚期","calving":"产犊","disease":"疫病","review":"待核对","unclassified":"未分类"}
PURPOSES = ["孕后期监测","产犊监测","产后监测","难产","死胎","正常监测","发情监测","孕早期监测","孕中期监测","疫病监测"]
VALIDITY = ["","待判定","有效","无效","/"]
DEFAULTS = {"host":"61.177.77.222","port":8022,"user":"cowmata_upload","server_file":r"F:\牛舍_现场记录\样本试验台账.csv","farm":"扬大_高邮牧场","wearer":"刘彦平","annotator":"张强","auto_sync":False,"sync_seconds":30,"minimize_to_tray":True}
def now(): return datetime.now(ZONE).isoformat(timespec="seconds")
def today(): return datetime.now(ZONE).date().isoformat()
def sha(data): return hashlib.sha256(data).hexdigest()
def empty_record(settings=None):
    s = {**DEFAULTS, **(settings or {})}
    row = dict.fromkeys(FIELDS, "")
    row.update({"记录ID":str(uuid.uuid4()),"记录日期":today(),"牧场":s["farm"],"监测目的":"产犊监测","数据分类":"calving","尾环佩戴人":s["wearer"],"事件标注人":s["annotator"],"已删除":"0"})
    return row
def category_for(purpose):
    return {"孕后期监测":"pregnancy_late","孕早期监测":"pregnancy_early","孕中期监测":"pregnancy_mid","产犊监测":"calving","产后监测":"calving","难产":"calving","死胎":"calving","发情监测":"estrus","疫病监测":"disease","正常监测":"healthy"}.get(purpose,"")

def classify_record(row):
    """Derived directory classification only; never modify original monitoring purpose."""
    purpose=str(row.get('监测目的','')).strip()
    def stamp(key):
        value=str(row.get(key,'') or '').strip()
        if key in ('配种开始','配种结束'):
            try:value=json.loads(row.get('原始单元格') or '{}').get('__extra__',{}).get(key,value)
            except (ValueError,AttributeError):pass
        if value in ('','/'):return None
        try:return datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:raise ValueError(key+'时间格式待核对')
    try:
        start,end=stamp('佩戴开始'),stamp('佩戴结束')
        birth,finish=stamp('产犊开始'),stamp('产犊结束')
        if birth or finish:
            if not birth or not finish:return 'review','产犊开始与结束时间未完整填写'
            if start and end and start<=birth<=finish<=end:return 'calving','产犊开始和结束均位于设备佩戴期间'
            return 'review','产犊时间不在完整佩戴区间内'
        mating_start,mating_end=stamp('配种开始'),stamp('配种结束')
        if mating_start or mating_end:
            if mating_start and mating_end and mating_start<=mating_end:return 'pregnancy_late','配种区间完整且未记录产犊开始或结束（按指定规则）'
            return 'review','配种开始与结束不完整或先后顺序有误'
    except ValueError as error:return 'review',str(error)
    explicit={'产犊':'calving','产犊监测':'calving','发情':'estrus','发情监测':'estrus','正常':'healthy','正常监测':'healthy','正常对照':'healthy','疫病':'disease','疫病监测':'disease','疾病监测':'disease','怀孕':'pregnancy','怀孕监测':'pregnancy','孕早期':'pregnancy_early','孕早期监测':'pregnancy_early','孕中期':'pregnancy_mid','孕中期监测':'pregnancy_mid','孕后期':'pregnancy_late','孕后期监测':'pregnancy_late','孕晚期':'pregnancy_late','孕晚期监测':'pregnancy_late'}
    code=explicit.get(purpose,'unclassified')
    if code=='calving' or purpose in ('产后监测','难产','死胎'):return 'review','未记录完整产犊时间，不能认定佩戴期间产犊'
    return code,'按原表明确监测目的归类；未以佩戴时间替代配种时间'

def clean_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "未填写"
def directory(row):
    cow = row.get("牛号","")
    match = re.fullmatch(r"(\d{5})(.*)",cow)
    ear,mark = (match[1],match[2]) if match else (cow,"")
    mark = row.get("现场标记","") or mark
    device = clean_name(row.get("设备号",""))
    identity = "-".join(clean_name(v) for v in [device,ear,mark] if v)
    category = CATEGORIES.get(row.get("数据分类"),"待分类")
    return "/".join([clean_name(row.get("牧场","")),category,row.get("记录日期") or "待核对日期","九轴",identity])
def normalize_time(value, date_only=False):
    value = str(value or "").strip()
    if value in ("","/"): return value
    value = value.replace("T"," ").replace("/","-")
    formats = ["%Y-%m-%d"] if date_only else ["%Y-%m-%d %H:%M","%Y-%m-%d %H:%M:%S","%Y-%m-%d %H"]
    for fmt in formats:
        try:
            dt = datetime.strptime(value,fmt)
            return dt.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M") + (dt.strftime(":%S") if not date_only and dt.second else "")
        except ValueError: pass
    raise ValueError("请用 YYYY-MM-DD" + ("" if date_only else " HH:mm，例 2026-09-15 09:00") + "；未发生可留空，不适用填 /")
def normalize(row):
    result = {f:str(row.get(f,"") or "").strip() for f in FIELDS}
    for f in ["预产期","记录日期"]:
        result[f] = normalize_time(result[f],True)
    for f in ["佩戴开始","佩戴结束","产犊开始","产犊结束"]:
        result[f] = normalize_time(result[f])
    result["设备号"] = result["设备号"].upper().replace(":","").replace("-","")
    result["归类目录"] = directory(result)
    return result
def issues(row, required=True):
    errors=[]
    if required:
        for f in ["牛号","设备号","佩戴开始","记录日期","牧场"]:
            if row.get(f,"") in ("","/"): errors.append(f+"未填写")
    if row.get("设备号") and not re.fullmatch("[0-9A-Fa-f]{12}",row["设备号"]): errors.append("设备号应为12位十六进制")
    for f in ["记录日期","预产期","佩戴开始","佩戴结束","产犊开始","产犊结束"]:
        try: normalize_time(row.get(f),f in ("记录日期","预产期"))
        except ValueError: errors.append(f+"格式待核对")
    for a,b in [("佩戴开始","佩戴结束"),("产犊开始","产犊结束")]:
        try:
            if row.get(a) not in ("",None,"/") and row.get(b) not in ("",None,"/"):
                if datetime.fromisoformat(row[b])<datetime.fromisoformat(row[a]): errors.append(b+"早于"+a)
        except ValueError: pass
    for f in ["九轴","脉搏","温度"]:
        if row.get(f,"") not in VALIDITY: errors.append(f+"请选择有效/无效/待判定")
    for f in ["尾环佩戴人","事件标注人"]:
        if len(row.get(f,""))>12 or row.get(f) in ("有效","无效"): errors.append(f+"可能错位")
    if row.get("数据分类") not in CATEGORIES: errors.append("请选择数据分类")
    return errors

SYNC_FIELDS=BUSINESS+["记录日期","牧场","数据分类","现场标记","核对提示","时间说明","已删除"]
def content_fingerprint(row):
    values={f:str(row.get(f,"") or "").strip() for f in SYNC_FIELDS}
    values["已删除"]=values["已删除"] or "0"
    values["设备号"]=values["设备号"].upper().replace(":","").replace("-","")
    for field in ["预产期","记录日期","佩戴开始","佩戴结束","产犊开始","产犊结束"]:
        try:values[field]=normalize_time(values[field],field in ("预产期","记录日期"))
        except ValueError:pass
    return sha(json.dumps(values,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf8"))
def event_identity(row):
    # A cow can have many trials. Only a complete, timed wearing event is a candidate.
    cow=str(row.get("牛号","")).strip().upper();farm=str(row.get("牧场","")).strip()
    device=str(row.get("设备号","")).upper().replace(":","").replace("-","")
    try:start=normalize_time(row.get("佩戴开始",""))
    except ValueError:return ""
    if not cow or not farm or start in ("","/") or not re.fullmatch("[0-9A-F]{12}",device):return ""
    return sha(json.dumps([farm,cow,device,start],ensure_ascii=False,separators=(",",":")).encode("utf8"))

def csv_bytes(rows):
    stream=io.StringIO(newline="")
    writer=csv.DictWriter(stream,fieldnames=FIELDS,lineterminator="\r\n")
    writer.writeheader()
    for row in sorted(rows,key=lambda r:(r.get("记录日期",""),r.get("牛号",""),r.get("佩戴开始",""),r.get("记录ID",""))):
        writer.writerow({f:row.get(f,"") for f in FIELDS})
    return stream.getvalue().encode("utf-8-sig")
def read_csv(data):
    reader=csv.DictReader(io.StringIO(data.decode("utf-8-sig"),newline=""),strict=True)
    if reader.fieldnames!=FIELDS: raise ValueError("CSV表头不匹配，请使用本软件导出的完整总表")
    result=list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in result): raise ValueError("CSV记录列数不匹配")
    return result
def atomic_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix="."+path.name+".",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        import time
        for attempt in range(6):
            try:
                os.replace(tmp,path);break
            except PermissionError:
                if attempt==5:raise
                time.sleep(0.025 * 2**attempt)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
class Store:
    def __init__(self,root,schema=None):
        self.schema=schema
        self.fields=schema.fields if schema else FIELDS
        self.defaults={**DEFAULTS,**(schema.defaults if schema else {"sheet_id":"samples"})}
        self.normalize=schema.normalize if schema else normalize
        self.issues=schema.issues if schema else issues
        self.directory=schema.directory if schema else directory
        self.fingerprint=schema.fingerprint if schema else content_fingerprint
        self.identity=schema.identity if schema else event_identity
        self.encode_csv=schema.csv_bytes if schema else csv_bytes
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.csv_path=self.root/(schema.filename if schema else "样本试验台账.csv")
        self.db=sqlite3.connect(self.root/"台账缓存.sqlite",timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,body TEXT NOT NULL,base TEXT NOT NULL DEFAULT '',dirty INTEGER NOT NULL DEFAULT 1,generation INTEGER NOT NULL DEFAULT 1,conflict TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS undo(id INTEGER PRIMARY KEY AUTOINCREMENT,record_id TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,message TEXT NOT NULL);""")
        if "held" not in {r[1] for r in self.db.execute("PRAGMA table_info(records)")}:
            self.db.execute("ALTER TABLE records ADD COLUMN held INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
        if "base_body" not in {r[1] for r in self.db.execute("PRAGMA table_info(records)")}:
            self.db.execute("ALTER TABLE records ADD COLUMN base_body TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE records SET base_body=body WHERE dirty=0 AND base!=''")
            self.db.commit()
        self.last_export_error=""
    def baseline(self,item):
        if item['base_body']:return json.loads(item['base_body'])
        if not item['base']:return None
        # Upgrade old caches using the last clean snapshot kept by undo.
        for entry in self.db.execute("SELECT body FROM undo WHERE record_id=? ORDER BY id",(item['id'],)):
            row=json.loads(entry[0])
            if row.get('版本')==item['base']:return row
        return None
    def reconcile_conflicts(self):
        from ledger_merge import merge_record,DeletedRecordConflict
        with self.db:
            for item in self.db.execute("SELECT * FROM records WHERE conflict IS NOT NULL").fetchall():
                remote=json.loads(item['conflict'])
                try:merged,_,_=merge_record(self.baseline(item),json.loads(item['body']),remote,self.schema)
                except DeletedRecordConflict:continue
                merged['版本']=remote['版本']
                self.db.execute("UPDATE records SET body=?,base=?,base_body=?,dirty=?,conflict=NULL WHERE id=?",(json.dumps(merged,ensure_ascii=False),remote['版本'],json.dumps(remote,ensure_ascii=False),int(self.fingerprint(merged)!=self.fingerprint(remote)),item['id']))
    def settings(self):
        return {**self.defaults,**{r["key"]:json.loads(r["value"]) for r in self.db.execute("SELECT * FROM settings")}}
    def configure(self,values):
        with self.db:
            for k,v in values.items(): self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v,ensure_ascii=False)))
    def log(self,message):
        with self.db:
            self.db.execute("INSERT INTO logs(at,message) VALUES(?,?)",(now(),str(message)[:4000]))
            self.db.execute("DELETE FROM logs WHERE id < (SELECT COALESCE(MAX(id),0)-500 FROM logs)")
    def get(self,record_id):
        item=self.db.execute("SELECT * FROM records WHERE id=?",(record_id,)).fetchone()
        if not item: return None
        row=json.loads(item["body"]);row["_dirty"]=item["dirty"];row["_conflict"]=item["conflict"];row["_held"]=item["held"];return row
    def all(self,deleted=False):
        result=[]
        for item in self.db.execute("SELECT * FROM records"):
            row=json.loads(item["body"])
            if not deleted and row.get("已删除")=="1": continue
            row.update(_dirty=item["dirty"],_conflict=item["conflict"],_held=item["held"])
            result.append(row)
        return result
    def _save(self,row,strict=True):
        old=self.db.execute("SELECT * FROM records WHERE id=?",(row["记录ID"],)).fetchone()
        if old and old["conflict"]: raise ValueError("此记录有服务器冲突，请先在“同步冲突”中处理")
        item=self.normalize(row) if strict else {f:str(row.get(f,"") or "") for f in self.fields}
        if strict:
            errors=self.issues(item)
            if errors: raise ValueError("；".join(errors))
            item["核对提示"]=""
        item["归类目录"]=self.directory(item)
        item["已删除"]=item["已删除"] or "0"
        if old and self.fingerprint(item)==self.fingerprint(json.loads(old["body"])):
            return json.loads(old["body"])
        item["修改时间"]=now()
        item["版本"]=old["base"] if old else ""
        item["已删除"]=item["已删除"] or "0"
        if old: self.db.execute("INSERT INTO undo(record_id,body) VALUES(?,?)",(row["记录ID"],old["body"]))
        self.db.execute("INSERT INTO records(id,body,base) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,dirty=1,generation=generation+1",(item["记录ID"],json.dumps(item,ensure_ascii=False),item["版本"]))
        return item
    def save(self,row,strict=True):
        with self.db: result=self._save(row,strict)
        self.export();return result
    def import_rows(self,rows,hold=False,hold_issues=False,review_updates=True,allow_dirty_ids=()):
        added=skipped=updated=conflicts=0
        current={r["记录ID"]:r for r in self.all(True)}
        fingerprints={};events={}
        def index(row):
            key=row["记录ID"]
            fingerprints.setdefault(self.fingerprint(row),set()).add(key)
            event=self.identity(row)
            if event:events.setdefault(event,set()).add(key)
        for row in current.values():index(row)
        with self.db:
            for incoming in rows:
                row=dict(incoming);fingerprint=self.fingerprint(row)
                old=current.get(row["记录ID"])
                if not old:
                    identical=fingerprints.get(fingerprint,set())
                    if identical:skipped+=1;continue
                    candidates=events.get(self.identity(row),set())
                    if len(candidates)>1:conflicts+=1;continue
                    if candidates:old=current[next(iter(candidates))]
                if old:
                    if self.fingerprint(old)==fingerprint:skipped+=1;continue
                    if (old["_dirty"] and old["记录ID"] not in allow_dirty_ids) or old["_conflict"]:
                        conflicts+=1;continue
                    # Imported changes to an existing event always require review.
                    fingerprints.get(self.fingerprint(old),set()).discard(old["记录ID"])
                    events.get(self.identity(old),set()).discard(old["记录ID"])
                    row["记录ID"]=old["记录ID"];row["版本"]=old["版本"]
                    self._save(row,False)
                    self.db.execute("UPDATE records SET held=? WHERE id=?",(int(review_updates),row["记录ID"]))
                    updated+=1
                else:
                    row["版本"]="";self._save(row,False)
                    if hold or (hold_issues and row.get("核对提示")):
                        self.db.execute("UPDATE records SET held=1 WHERE id=?",(row["记录ID"],))
                    added+=1
                current[row["记录ID"]]=self.get(row["记录ID"]);index(current[row["记录ID"]])
        self.last_import_summary={"added":added,"skipped":skipped,"updated":updated,"conflicts":conflicts}
        self.export();return added,skipped
    def export(self):
        try:
            content=self.encode_csv(self.all(True))
            if not self.csv_path.exists() or self.csv_path.read_bytes()!=content:atomic_write(self.csv_path,content)
            self.last_export_error=""
        except OSError as e: self.last_export_error="本地记录已保存，CSV被占用；关闭Excel后会自动重试："+str(e)
    def delete(self,ids):
        with self.db:
            for key in ids:
                row=self.get(key);row["已删除"]="1";self._save(row,False)
        self.export()
    def undo(self):
        item=self.db.execute("SELECT * FROM undo ORDER BY id DESC LIMIT 1").fetchone()
        if not item: raise ValueError("没有可撤销的修改")
        with self.db:
            self._save(json.loads(item["body"]),False)
            self.db.execute("DELETE FROM undo WHERE id>=?",(item["id"],))
        self.export()
    def pending(self):
        return [{"record":json.loads(r["body"]),"base":r["base"],"generation":r["generation"],"base_record":self.baseline(r)} for r in self.db.execute("SELECT * FROM records WHERE dirty=1 AND conflict IS NULL AND held=0")]
    def release_review(self,ids):
        with self.db:
            for key in ids:self.db.execute("UPDATE records SET held=0 WHERE id=?",(key,))
    def known_ids(self):
        return [r[0] for r in self.db.execute("SELECT id FROM records WHERE base != ''")]
    def apply_sync(self,server_rows,sent,aliases=None,accepted=None):
        from ledger_merge import merge_record,DeletedRecordConflict
        remote={r["记录ID"]:r for r in server_rows}
        missing=set(self.known_ids())-set(remote)
        if missing:raise ValueError(f"服务器总表缺少{len(missing)}条已同步记录，已停止同步；本地完整副本保留，请检查服务器文件")
        aliases=aliases or {}
        sent=[{**p,"record":dict(p["record"])} for p in sent]
        for source,destination in aliases.items():
            if destination not in remote:raise ValueError("服务器去重映射无效")
        with self.db:
            for source,destination in aliases.items():
                if source==destination:continue
                local=self.db.execute("SELECT * FROM records WHERE id=?",(source,)).fetchone()
                existing=self.db.execute("SELECT * FROM records WHERE id=?",(destination,)).fetchone()
                if local:
                    if existing:
                        # Preserve both local edits as a visible conflict if they differ.
                        if local["dirty"] and self.fingerprint(json.loads(local["body"]))!=self.fingerprint(json.loads(existing["body"])):
                            raise ValueError("重复记录与本地修改冲突，请先核对后重新同步")
                        self.db.execute("DELETE FROM records WHERE id=?",(source,))
                    else:
                        body=json.loads(local["body"]);body["记录ID"]=destination
                        self.db.execute("UPDATE records SET id=?,body=? WHERE id=?",(destination,json.dumps(body,ensure_ascii=False),source))
                    self.db.execute("DELETE FROM undo WHERE record_id=?",(source,))
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_rows'").fetchone():
                        for link in self.db.execute("SELECT path,slot,body FROM source_rows WHERE record_id=?",(source,)).fetchall():
                            baseline=json.loads(link['body']);baseline['记录ID']=destination
                            self.db.execute('UPDATE source_rows SET record_id=?,body=? WHERE path=? AND slot=?',(destination,json.dumps(baseline,ensure_ascii=False),link['path'],link['slot']))
                for change in sent:
                    if change["record"]["记录ID"]==source:change["record"]["记录ID"]=destination
            snapshots={p["record"]["记录ID"]:p for p in sent}

            for key,row in remote.items():
                local=self.db.execute("SELECT * FROM records WHERE id=?",(key,)).fetchone()
                body=json.dumps(row,ensure_ascii=False);version=row["版本"]
                if not local:
                    self.db.execute("INSERT INTO records(id,body,base,dirty) VALUES(?,?,?,0)",(key,body,version));self.db.execute("UPDATE records SET base_body=? WHERE id=?",(body,key));continue
                submitted=snapshots.get(key)
                same_content=submitted and (key in accepted if accepted is not None else self.fingerprint(row)==self.fingerprint(submitted["record"]))
                if same_content:
                    if local["generation"]==submitted["generation"]:
                        self.db.execute("UPDATE records SET body=?,base=?,dirty=0,held=0,conflict=NULL WHERE id=?",(body,version,key))
                    else:
                        latest,_,_=merge_record(submitted['record'],json.loads(local['body']),row,self.schema);latest["版本"]=version
                        self.db.execute("UPDATE records SET body=?,base=?,conflict=NULL WHERE id=?",(json.dumps(latest,ensure_ascii=False),version,key))
                elif self.fingerprint(json.loads(local["body"]))==self.fingerprint(row):
                    # A lost acknowledgement or another client may already have saved this draft.
                    if not local["dirty"] and not local["held"] and not local["conflict"] and local["base"]==version:continue
                    latest=json.loads(local["body"]);latest["版本"]=version
                    self.db.execute("UPDATE records SET body=?,base=?,dirty=0,held=0,conflict=NULL WHERE id=?",(json.dumps(latest,ensure_ascii=False),version,key))
                elif not local["dirty"]:
                    if local["base"]==version and self.fingerprint(json.loads(local["body"]))==self.fingerprint(row) and not local["conflict"]:continue
                    self.db.execute("UPDATE records SET body=?,base=?,conflict=NULL WHERE id=?",(body,version,key))
                elif local["base"]!=version:
                    try:
                        latest,_,_=merge_record(self.baseline(local),json.loads(local['body']),row,self.schema)
                    except DeletedRecordConflict:
                        self.db.execute("UPDATE records SET conflict=? WHERE id=?",(body,key));continue
                    latest['版本']=version
                    self.db.execute("UPDATE records SET body=?,base=?,dirty=?,conflict=NULL WHERE id=?",(json.dumps(latest,ensure_ascii=False),version,int(self.fingerprint(latest)!=self.fingerprint(row)),key))
                if local['base_body']!=body:
                    self.db.execute("UPDATE records SET base_body=? WHERE id=?",(body,key))
        self.export()
    def resolve(self,key,keep_local):
        local=self.db.execute("SELECT * FROM records WHERE id=?",(key,)).fetchone()
        if not local or not local["conflict"]: return
        remote=json.loads(local["conflict"]);row=json.loads(local["body"]) if keep_local else remote
        row["版本"]=remote["版本"]
        if keep_local: row["修改时间"]=now()
        with self.db:
            self.db.execute("UPDATE records SET body=?,base=?,base_body=?,dirty=?,generation=generation+1,conflict=NULL WHERE id=?",(json.dumps(row,ensure_ascii=False),remote["版本"],json.dumps(remote,ensure_ascii=False),int(keep_local),key))
        self.export()
