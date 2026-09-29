"""Bounded SSH transport; v2 never falls back to uploading disguised Excel."""
import base64,json,subprocess
from pathlib import Path
from .ledger_core import FIELDS,read_csv,sha,content_fingerprint,event_identity
from .ledger_direct import DirectBridge,DirectError,clean_environment
from .ledger_auth import key_path
class SyncError(Exception): pass
# Host hooks: the Annotator supplies its bundled SSH, connection identity and
# pinned host key, and signs requests with its own ('pro') session product.
SECURITY_PRODUCT=None
def transport_paths(root):
    root=Path(root)
    return root/"transport"/"usr"/"bin"/"ssh.exe",key_path(root),root/"known_hosts"
def request_json(root,settings,request):
    root=Path(root)
    executable,key,hosts=transport_paths(root)
    if SECURITY_PRODUCT:request={**request,"security_product":SECURITY_PRODUCT}
    for p in [executable,key,hosts]:
        if not p.is_file(): raise SyncError("缺少连接组件："+str(p))
    data=json.dumps(request,ensure_ascii=False,separators=(",",":")).encode("utf8")
    if len(data)>16*1024*1024: raise SyncError("待同步内容超过16MB，请联系管理员调整接收限制")
    try:
        with DirectBridge(settings["host"],settings["port"]) as bridge:
            # Alias pins the real server key even though SSH uses our private local bridge.
            alias="["+settings["host"]+"]:"+str(settings["port"])
            args=[str(executable),"-F","none","-T",
                  "-o","ProxyCommand=none","-o","ProxyJump=none","-o","ClearAllForwardings=yes",
                  "-o","BatchMode=yes","-o","IdentitiesOnly=yes","-o","StrictHostKeyChecking=yes",
                  "-o","HostKeyAlias="+alias,"-o","ConnectTimeout=10",
                  "-o","ServerAliveInterval=10","-o","ServerAliveCountMax=2",
                  "-o","UserKnownHostsFile="+hosts.name,"-i",str(key),
                  "-p",str(bridge.port),settings["user"]+"@127.0.0.1","cowmata-ledger-upload-v2"]
            result=subprocess.run(args,input=data+b"\n",stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  timeout=50,env=clean_environment(),cwd=hosts.parent,
                                  creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            connection=bridge.info
    except DirectError as e:raise SyncError(str(e)) from e
    except subprocess.TimeoutExpired as e: raise SyncError("连接超时，本地数据已保留，请点击同步重试") from e
    except OSError as e: raise SyncError("SSH无法启动："+str(e)) from e
    output=result.stdout.decode("utf8","replace").strip()
    try: reply=json.loads(output)
    except ValueError: raise SyncError("服务器未返回有效确认。"+result.stderr.decode("utf8","replace")[-800:])
    if reply.get("version")!=2:
        raise SyncError("服务器尚未升级单文件CSV接收端。请安装 server-upgrade；本地记录已保存。")
    if not reply.get("ok"): raise SyncError(reply.get("code","")+"："+reply.get("error","服务器拒绝此次提交"))
    reply["connection"]=connection
    return reply

def account_request(root,settings,action,**payload):
    from cowmata_security.client import current_session,AccessDenied
    from . import ledger_session
    session=current_session()
    try:
        if action=="whoami":
            session.refresh();return {"version":2,"ok":True,**ledger_session.current()}
        if action=="logout":
            session.logout();return {"version":2,"ok":True}
        raise AccessDenied("旧账号接口已关闭，请使用账号与一次性码管理")
    except AccessDenied as exc:raise SyncError("AUTH_REQUIRED："+str(exc)) from None

def _exchange(root,settings,pending,action="sync",known_ids=None):
    from .ledger_session import token
    request={"sheet_id":settings.get("sheet_id","samples"),"version":2,"action":action,"target":settings["server_file"],"changes":pending,"known_ids":known_ids or [],"session_token":token()}
    reply=request_json(root,settings,request)
    if reply.get("target")!=settings["server_file"]:raise SyncError("服务器保存路径与客户端设置不一致")
    if action in ("probe","inventory"):return reply
    try: content=base64.b64decode(reply["content"],validate=True)
    except (KeyError,ValueError) as e: raise SyncError("服务器返回CSV内容无效") from e
    if sha(content)!=reply.get("sha256"): raise SyncError("服务器CSV完整性校验失败，未标记同步成功")
    from .ledger_sheets import get_schema
    schema=get_schema(settings.get("sheet_id","samples"))
    reply["rows"]=(schema.read_csv if schema else read_csv)(content)
    return reply


def synchronize(root,settings,pending,action="sync",known_ids=None):
    from .ledger_sheets import get_schema
    schema=get_schema(settings.get("sheet_id","samples"))
    fingerprint=schema.fingerprint if schema else content_fingerprint
    identity=schema.identity if schema else event_identity
    if action!="sync" or not pending:
        return _exchange(root,settings,pending,action,known_ids)
    # Send fingerprints first. Already present records never enter the upload body.
    inventory=_exchange(root,settings,[],"inventory",known_ids).get("inventory")
    if not isinstance(inventory,list):raise SyncError("服务器未返回增量比对清单，本地数据保留")
    ids={};fingerprints={};events={}
    for item in inventory:
        ids[item["id"]]=item
        fingerprints.setdefault(item["fingerprint"],[]).append(item)
        if item.get("event"):events.setdefault(item["event"],[]).append(item)
    delta=[];aliases={};skipped=0;skipped_ids=[];remote_rows=None;claimed=set()
    for change in pending:
        r=change['record']
        if r['记录ID'] in ids:claimed.add(r['记录ID'])
        else:
            candidates=fingerprints.get(fingerprint(r),[]) or events.get(identity(r),[])
            if len(candidates)==1:claimed.add(candidates[0]['id'])
    for change in pending:
        row=dict(change["record"]);key=row["记录ID"];match=ids.get(key)
        if not match and not change["base"]:
            candidates=fingerprints.get(fingerprint(row),[]) or events.get(identity(row),[])
            if len(candidates)>1:raise SyncError("服务器有多条可能重复的试验记录，请先核对")
            if candidates:
                match=candidates[0];aliases[key]=match["id"];row["记录ID"]=match["id"]
        base=change['base']
        if not base and row.get('来源') and (not match or match['fingerprint']!=fingerprint(row)):
            if remote_rows is None:remote_rows=_exchange(root,settings,[],'pull',known_ids)['rows']
            from .ledger_files import slot
            position=slot(row);filename=row['来源'].split(' / ')[0]
            source=[r for r in remote_rows if r.get('来源','').split(' / ')[0]==filename and slot(r)[0]==position[0]]
            serial=[r for r in source if row.get('序号') and r.get('序号')==row['序号']] if schema is None else []
            candidates=serial or [r for r in source if slot(r)==position]
            if not candidates:
                from .ledger_matching import continuation
                candidates=continuation(row,remote_rows,schema)
            candidates=[r for r in candidates if r['记录ID'] not in claimed or match and r['记录ID']==match['id']]
            if len(candidates)>1:raise SyncError('原文件对应多条服务器记录，请先读取服务器台账核对')
            if candidates:
                selected=candidates[0];match=ids[selected['记录ID']];aliases[key]=match['id'];row['记录ID']=match['id'];base=match['version'];claimed.add(match['id'])
        if match and match['fingerprint']==fingerprint(row):
            skipped+=1;skipped_ids.append(row['记录ID']);continue
        baseline=change.get('base_record')
        if match:
            if remote_rows is None:remote_rows=_exchange(root,settings,[],'pull',known_ids)['rows']
            current=next((r for r in remote_rows if r['记录ID']==row['记录ID']),None)
            if current is None:raise SyncError('服务器记录在比对时消失，本地修改保留')
            from .ledger_merge import merge_record,DeletedRecordConflict
            try:
                row,_,_=merge_record(baseline,row,current,schema)
                base=current['版本'];baseline=current
            except DeletedRecordConflict:
                # Preserve deletion conflicts for an explicit administrator choice.
                pass
        if baseline is not None:baseline={**baseline,'记录ID':row['记录ID']}
        delta.append({**change,"record":row,"base":base,"base_record":baseline})
    reply=_exchange(root,settings,delta,"sync",known_ids)
    reply["aliases"]={**aliases,**reply.get("aliases",{})}
    server_ids={r['记录ID'] for r in reply['rows']}
    accepted=reply.get('accepted');conflicts=reply.get('conflicts')
    if not isinstance(accepted,list) or not isinstance(conflicts,list):raise SyncError('服务器未返回逐条确认，保留本地修改等待重试')
    expected={reply['aliases'].get(p['record']['记录ID'],p['record']['记录ID']) for p in delta}
    if set(accepted)&set(conflicts) or set(accepted)|set(conflicts)!=expected or not set(accepted)<=server_ids:
        raise SyncError('服务器逐条回执不完整，保留本地修改等待重试')
    reply['accepted']=list(dict.fromkeys(accepted+skipped_ids))
    reply['delta']['confirmed']=len(reply['accepted'])
    reply['delta']['unconfirmed']=len(conflicts)
    reply.setdefault("delta",{}).update({"preflight_skipped":skipped,"uploaded":len(delta)})
    return reply
