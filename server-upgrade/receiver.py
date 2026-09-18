"""Restricted SSH v2 receiver. The only persistent data file is the fixed CSV."""
import base64,ctypes,json,os,re,subprocess,sys,uuid,threading
from contextlib import contextmanager
from pathlib import Path
from ledger_core import FIELDS,atomic_write,csv_bytes,read_csv,sha,content_fingerprint,event_identity
MAX_REQUEST=16*1024*1024
DEFAULT_TARGET=r"F:\牛舍_现场记录\样本试验台账.csv"
@contextmanager
def locked(target):
    # Derive the name lexically: resolving a path while its file is being
    # atomically replaced can return a different identity on Windows.
    lock_identity=os.path.normcase(os.path.abspath(os.fspath(target)))
    if os.name=="nt":
        from ctypes import wintypes
        api=ctypes.WinDLL("kernel32",use_last_error=True)
        api.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];api.CreateMutexW.restype=wintypes.HANDLE
        api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];api.WaitForSingleObject.restype=wintypes.DWORD
        api.ReleaseMutex.argtypes=[wintypes.HANDLE];api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.CreateMutexW(None,False,"Global\\CowmataLedger-"+sha(lock_identity.encode())[:32])
        if not handle: raise OSError(ctypes.get_last_error(),"无法创建台账写入互斥锁")
        acquired=False
        try:
            status=api.WaitForSingleObject(handle,15000)
            if status not in (0,0x80): raise TimeoutError("其他用户正在写入，请重试")
            acquired=True;yield
        finally:
            if acquired: api.ReleaseMutex(handle)
            api.CloseHandle(handle)
    else:
        # POSIX test/development only: stable lock outside the data directory.
        import fcntl,tempfile
        with open(Path(tempfile.gettempdir())/("cowmata-"+sha(lock_identity.encode())+".lock"),"a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);yield
def validate_record(row,fields=FIELDS):
    if not isinstance(row,dict) or set(row)!=set(fields): raise ValueError("记录字段不完整")
    if any(not isinstance(v,str) or len(v)>65536 or "\0" in v for v in row.values()): raise ValueError("字段格式无效或过长")
    if sum(map(len,row.values()))>100000: raise ValueError("单条记录过长")
    uuid.UUID(row["记录ID"])
    if row["已删除"] not in ("0","1"): raise ValueError("删除状态无效")
def equivalent(a,b):
    return content_fingerprint(a)==content_fingerprint(b)
def handle(request,target=DEFAULT_TARGET,schema=None,allow_delete=True):
    sheet_id=request.get("sheet_id","samples") if isinstance(request,dict) else ""
    if sheet_id!=(schema.id if schema else "samples"):raise ValueError("Sheet与服务器目标不匹配")
    fields=schema.fields if schema else FIELDS
    decode=schema.read_csv if schema else read_csv
    encode=schema.csv_bytes if schema else csv_bytes
    fingerprint=schema.fingerprint if schema else content_fingerprint
    identity=schema.identity if schema else event_identity
    if not isinstance(request,dict) or request.get("version")!=2: raise ValueError("不支持的协议")
    if request.get("target",target)!=str(target): raise ValueError("目标路径与服务器固定路径不一致")
    action=request.get("action")
    if action not in ("probe","sync","pull","inventory"): raise ValueError("只允许检测和台账同步")
    target=Path(target)
    if action=="probe": return {"version":2,"ok":True,"target":str(target),"protocol":"cowmata-ledger-upload-v2"}
    changes=request.get("changes",[]) if action=="sync" else []
    if not isinstance(changes,list) or len(changes)>20000: raise ValueError("变更数量超限")
    seen=set()
    for change in changes:
        if not isinstance(change,dict) or not isinstance(change.get("base"),str): raise ValueError("变更格式无效")
        validate_record(change.get("record"),fields)
        if not allow_delete and change["record"]["已删除"]=="1":
            from ledger_accounts import AccessError
            raise AccessError("操作员不能删除台账","FORBIDDEN")
        baseline=change.get('base_record')
        if baseline is not None:
            validate_record(baseline,fields)
            if baseline['记录ID']!=change['record']['记录ID'] or baseline['版本']!=change['base']:raise ValueError('单元格基线与记录版本不符')
        key=change["record"]["记录ID"]
        if key in seen: raise ValueError("重复记录ID")
        seen.add(key)
    with locked(target):
        existed=target.exists()
        if existed and target.stat().st_size>64*1024*1024:raise ValueError("服务器CSV超过64MB，请管理员归档或扩容")
        records=decode(target.read_bytes()) if existed else []
        existing={r["记录ID"]:r for r in records}
        if len(existing)!=len(records): raise ValueError("服务器CSV有重复记录ID，请管理员核对")
        for row in records: validate_record(row,fields)
        known=request.get("known_ids",[])
        if not isinstance(known,list) or any(not isinstance(key,str) for key in known):raise ValueError("已知记录列表无效")
        if set(known)-set(existing):raise ValueError("服务器总表文件缺失或记录不完整，已停止写入；本地记录保留，请管理员恢复已验证副本")
        if action=="inventory":
            return {"version":2,"ok":True,"target":str(target),"inventory":[
                {"id":r["记录ID"],"version":r["版本"],"fingerprint":fingerprint(r),"event":identity(r)} for r in records]}
        conflicts=[];accepted=[];modified=False;aliases={}
        inserted=updated=unchanged=merged_cells=overwritten_cells=0
        fingerprints={};events={}
        def index(row):
            key=row["记录ID"]
            fingerprints.setdefault(fingerprint(row),set()).add(key)
            event=identity(row)
            if event:events.setdefault(event,set()).add(key)
        for row in records:index(row)
        for change in changes:
            row=dict(change["record"]);key=row["记录ID"];old=existing.get(key)
            if not old and not change["base"]:
                same=fingerprints.get(fingerprint(row),set())
                candidates=same or events.get(identity(row),set())
                if len(candidates)==1:
                    canonical=next(iter(candidates));aliases[key]=canonical
                    key=canonical;row["记录ID"]=key;old=existing[key]
                elif candidates:raise ValueError("发现多条可能重复的试验记录，请先核对；本次未写入")
            if old and old["已删除"]!=row["已删除"] and not allow_delete:
                from ledger_accounts import AccessError
                raise AccessError("只有管理员可以删除或恢复台账","FORBIDDEN")
            if old and fingerprint(old)==fingerprint(row):
                accepted.append(key);unchanged+=1;continue
            if not old and change["base"]:
                raise ValueError("服务器缺少已同步记录，停止写入；请管理员检查总表完整性")
            if old and change["base"]!=old["版本"]:
                baseline=change.get('base_record')
                if baseline is None:conflicts.append(key);continue
                from ledger_merge import merge_record,DeletedRecordConflict
                try:row,cells,overwritten=merge_record(baseline,row,old,schema)
                except DeletedRecordConflict:conflicts.append(key);continue
                merged_cells+=len(cells);overwritten_cells+=len(overwritten)
                if fingerprint(old)==fingerprint(row):accepted.append(key);unchanged+=1;continue
            if old:
                updated+=1
                fingerprints.get(fingerprint(old),set()).discard(key)
                events.get(identity(old),set()).discard(key)
            else:inserted+=1
            row["版本"]=str(uuid.uuid4());existing[key]=row;accepted.append(key);modified=True;index(row)
        content=encode(existing.values())
        if len(content)>64*1024*1024:raise ValueError("服务器CSV容量超过64MB，本次未写入")
        if modified:
            atomic_write(target,content)
            # Acknowledgement refers to bytes re-read after the atomic replacement.
            content=target.read_bytes()
        elif existed: content=target.read_bytes()
        return {"version":2,"ok":True,"target":str(target),"sha256":sha(content),"content":base64.b64encode(content).decode("ascii"),"accepted":accepted,"conflicts":conflicts,"count":len(existing),"aliases":aliases,"delta":{"received":len(changes),"inserted":inserted,"updated":updated,"unchanged":unchanged,"merged_cells":merged_cells,"overwritten_cells":overwritten_cells}}
def dispatch(request,config,peer="unknown"):
    from ledger_accounts import Accounts,AccessError
    if not isinstance(request,dict) or request.get("version")!=2:raise AccessError("请升级客户端并登录")
    action=request.get("action")
    if action=="security":
        from cowmata_security.receiver_adapter import handle_security
        return handle_security(request,config,peer)
    if config.get("security_required"):
        from cowmata_security.receiver_adapter import ledger_principal
        from ledger_sheets import get_schema
        principal=ledger_principal(request,config)
        schema=get_schema(request.get("sheet_id","samples"))
        target=config.get("target",DEFAULT_TARGET) if schema is None else schema.defaults["server_file"]
        reply=handle(request,target,schema,allow_delete=principal["role"]=="admin")
        reply["sheet_id"]=request.get("sheet_id","samples");reply["user"]=principal
        return reply
    if action!="login" and not request.get("session_token"):raise AccessError("请升级至1.2.0并登录后使用台账")
    path=config.get("auth_db")
    if not path:raise AccessError("服务器账号尚未配置，请联系管理员")
    accounts=Accounts(path)
    try:
        if action=="login":
            return {"version":2,"ok":True,**accounts.login(request.get("username"),request.get("password"),peer)}
        token=request["session_token"];principal=accounts.principal(token);actor=principal["username"]
        admin=principal["role"]=="admin"
        if action=="whoami":return {"version":2,"ok":True,"user":principal}
        if action=="logout":
            accounts.logout(token,actor);return {"version":2,"ok":True}
        if action=="password_change":
            accounts.change_password(principal,request.get("old_password"),request.get("new_password"));return {"version":2,"ok":True}
        if action in ("accounts_list","account_create","account_update","audit_list"):
            if not admin:raise AccessError("仅管理员可以管理账号","FORBIDDEN")
            if action=="accounts_list":return {"version":2,"ok":True,"accounts":accounts.list()}
            if action=="audit_list":return {"version":2,"ok":True,"events":accounts.audit_entries()}
            if action=="account_create":accounts.create(request.get("username"),request.get("password"),request.get("role"),request.get("display_name",""),actor)
            else:accounts.update(request.get("username"),{key:request[key] for key in ("disabled","role","new_password","display_name") if key in request},actor)
            return {"version":2,"ok":True}
        from ledger_sheets import get_schema
        sheet_id=request.get("sheet_id","samples");schema=get_schema(sheet_id)
        target=config.get("target",DEFAULT_TARGET) if schema is None else schema.defaults["server_file"]
        reply=handle(request,target,schema,allow_delete=admin);reply["sheet_id"]=sheet_id;reply["user"]=principal
        if action=="sync" and request.get("changes"):
            accounts.audit(actor,"sync",{"sheet":sheet_id,"delta":reply.get("delta"),"accepted":reply.get("accepted",[]),"peer":peer})
        return reply
    finally:accounts.close()

def main():
    watchdog=threading.Timer(60,lambda:os._exit(124));watchdog.daemon=True;watchdog.start()
    home=Path(__file__).resolve().parent
    config=json.loads((home/"receiver-config.json").read_text("utf-8-sig")) if (home/"receiver-config.json").exists() else {}
    original=os.environ.get("SSH_ORIGINAL_COMMAND","")
    if original=="cowmata-ledger-upload-v1":
        print(json.dumps({"version":2,"ok":False,"code":"AUTH_REQUIRED","error":"旧协议已停用，请升级并登录"}));return 1
    if original!="cowmata-ledger-upload-v2":
        print(json.dumps({"version":2,"ok":False,"error":"仅允许台账上传协议"}));return 1
    try:
        raw=sys.stdin.buffer.read(MAX_REQUEST+1)
        if len(raw)>MAX_REQUEST: raise ValueError("请求超过16MB")
        request=json.loads(raw)
        peer=os.environ.get("SSH_CONNECTION","unknown").split()[0]
        reply=dispatch(request,config,peer)
    except Exception as e:
        reply={"version":2,"ok":False,"code":getattr(e,"code","REQUEST_FAILED"),"error":str(e)}
    sys.stdout.buffer.write(json.dumps(reply,ensure_ascii=False,separators=(",",":")).encode("utf8")+b"\n")
    return 0 if reply["ok"] else 1
if __name__=="__main__": sys.exit(main())
