"""Explicit, cell-level conflict review. Reading the queue never opens source files."""
import json
from .ledger_core import BUSINESS,normalize_time,classify_record,now
from .ledger_merge import merge_record,DeletedRecordConflict
from .ledger_files import init,extra_values,field_value,edit_extra,slot

def _fields(store,a,b):
 keys=list(dict.fromkeys((store.schema.business if store.schema else BUSINESS)+sorted(set(extra_values(a))|set(extra_values(b)))+['已删除']))
 return [k for k in keys if field_value(a,k)!=field_value(b,k)]

def source_issue(path,reason='旧版冲突标记需要重新读取原文件核对。'):
 return dict(kind='legacy',key=path,path=path,local={},other={},fields=[],reason=reason,source=path)

def review_items(store):
 init(store);result=[]
 for entry in store.db.execute('SELECT * FROM records WHERE conflict IS NOT NULL'):
  local=json.loads(entry['body']);other=json.loads(entry['conflict'])
  try:proposed,_,_=merge_record(store.baseline(entry),local,other,store.schema)
  except DeletedRecordConflict:proposed=local
  result.append(dict(kind='server',key=entry['id'],local=local,other=other,proposed=proposed,fields=_fields(store,local,other),expected_body=entry['body'],expected_conflict=entry['conflict'],reason='服务器与本机记录不同，请核对下列字段。',source=local.get('来源','') or '服务器记录'))
 for entry in store.db.execute('SELECT * FROM source_conflicts'):
  local=store.get(entry['record_id']);other=json.loads(entry['incoming'])
  if not local or local.get('_conflict'):continue
  body=store.db.execute('SELECT body FROM records WHERE id=?',(entry['record_id'],)).fetchone()[0]
  prior=store.db.execute('SELECT body FROM source_rows WHERE path=? AND record_id=?',(entry['path'],entry['record_id'])).fetchone()
  try:proposed,_,_=merge_record(json.loads(prior[0]) if prior else None,other,local,store.schema)
  except DeletedRecordConflict:proposed=local
  result.append(dict(kind='file',key=entry['record_id'],path=entry['path'],local=local,other=other,proposed=proposed,fields=_fields(store,local,other),expected_body=body,expected_incoming=entry['incoming'],reason='原文件与台账不同，请核对后保存本条。',source=other.get('来源','') or entry['path']))
 for entry in store.db.execute('SELECT * FROM source_files'):
  meta=json.loads(entry['document']);details={x['source']:x for x in meta.get('ambiguous_details',[])}
  for source in meta.get('ambiguous_rows',[]):
   detail=details.get(source)
   if detail:
    other=detail['incoming'];candidates=[store.get(k) for k in detail['candidates']];candidates=[r for r in candidates if r]
    proposals={}
    for candidate in candidates:
     try:proposals[candidate['记录ID']],_,_=merge_record(None,other,candidate,store.schema)
     except DeletedRecordConflict:proposals[candidate['记录ID']]=candidate
    result.append(dict(kind='ambiguous',key=source,path=entry['path'],local=other,other=other,fields=list(store.schema.business if store.schema else BUSINESS),candidates=candidates,proposals=proposals,expected_detail=detail,reason='这一行匹配到多条记录，请选择它对应的台账行，再核对最终内容。',source=source))
   else:result.append(dict(kind='legacy',key=source,path=entry['path'],local={},other={},fields=[],reason='旧版只保存了行位置。点击“后台重新核对”读取这一行，再选择对应记录。',source=source))
  represented=store.db.execute('SELECT COUNT(*) FROM source_conflicts c JOIN records r ON r.id=c.record_id WHERE c.path=?',(entry['path'],)).fetchone()[0]+len(meta.get('ambiguous_rows',[]))
  if meta.get('unresolved_conflicts',0)>represented:result.append(source_issue(entry['path']))
 return result

def save_review(store,item,values,admin=True,target_key=None):
 init(store);kind=item['kind']
 if kind=='legacy':raise ValueError('请先在后台重新核对文件')
 key=target_key if kind=='ambiguous' else item['key']
 if not key:raise ValueError('请先选择对应的台账行')
 with store.db:
  entry=store.db.execute('SELECT * FROM records WHERE id=?',(key,)).fetchone()
  if not entry:raise ValueError('记录已变化，请刷新核对面板')
  local=json.loads(entry['body']);other=item['other']
  if kind=='ambiguous':
   expected=next((r for r in item['candidates'] if r['记录ID']==key),None)
   if not expected or any(local.get(k,'')!=expected.get(k,'') for k in store.fields) or entry['conflict']:raise ValueError('候选记录已变化，请刷新核对面板')
   meta=json.loads(store.db.execute('SELECT document FROM source_files WHERE path=?',(item['path'],)).fetchone()[0])
   if item['expected_detail'] not in meta.get('ambiguous_details',[]):raise ValueError('文件核对内容已变化，请刷新')
   bindings=store.db.execute('SELECT slot FROM source_rows WHERE path=? AND record_id=?',(item['path'],key)).fetchall()
   if any(tuple(json.loads(b[0]))!=slot(other) for b in bindings):raise ValueError('该台账行已对应文件中的另一行，请选择其他记录或修改原文件后重新核对')
  elif entry['body']!=item['expected_body'] or (kind=='server' and entry['conflict']!=item['expected_conflict']):raise ValueError('记录已变化，请刷新核对面板')
  elif kind=='file':
   conflict=store.db.execute('SELECT incoming FROM source_conflicts WHERE path=? AND record_id=?',(item['path'],key)).fetchone()
   if entry['conflict'] or not conflict or conflict[0]!=item['expected_incoming']:raise ValueError('冲突已变化，请刷新核对面板')
  row=dict(local);allowed=set(store.schema.business if store.schema else BUSINESS)|set(extra_values(local))|set(extra_values(other))|{'已删除'}
  for field,value in values.items():
   if field not in allowed:raise ValueError('此字段不可编辑：'+field)
   value=str(value).strip()
   if field in extra_values(local) or field in extra_values(other):edit_extra(row,field,value)
   else:row[field]=value
  if row.get('已删除','0') not in ('0','1'):raise ValueError('已删除只能填 0（保留）或 1（删除）')
  if not admin and (row.get('已删除','0')!=local.get('已删除','0') or other.get('已删除')=='1'):
   if not (kind=='server' and other.get('已删除')=='1' and row.get('已删除')=='1'):raise ValueError('只有管理员可以删除或恢复记录')
  if store.schema:row=store.schema.normalize(row)
  else:
   for field in values:
    if field in ('预产期','佩戴开始','佩戴结束','产犊开始','产犊结束'):row[field]=normalize_time(row[field],field=='预产期')
   row['数据分类']=classify_record(row)[0]
  new_issues=[x for x in store.issues(row) if x not in store.issues(local)]
  if new_issues and row.get('已删除')!='1':raise ValueError('；'.join(new_issues))
  row['核对提示']='；'.join(store.issues(row));row['记录ID']=key;row['归类目录']=store.directory(row)
  if kind=='server':
   # Accepting a remote deletion is always the remote record, never a new operator write.
   if not admin and other.get('已删除')=='1':row={k:other.get(k,'') for k in store.fields}
   row['版本']=other['版本'];row['修改时间']=now()
   store.db.execute('INSERT INTO undo(record_id,body) VALUES(?,?)',(key,entry['body']))
   store.db.execute('UPDATE records SET body=?,base=?,base_body=?,dirty=?,generation=generation+1,conflict=NULL,held=0 WHERE id=?',(json.dumps(row,ensure_ascii=False),other['版本'],json.dumps(other,ensure_ascii=False),int(store.fingerprint(row)!=store.fingerprint(other)),key))
  else:
   store._save(row,False)
   incoming={**other,'记录ID':key}
   store.db.execute('DELETE FROM source_rows WHERE path=? AND record_id=?',(item['path'],key))
   store.db.execute('INSERT OR REPLACE INTO source_rows VALUES(?,?,?,?)',(item['path'],json.dumps(slot(other),ensure_ascii=False),key,json.dumps(incoming,ensure_ascii=False)))
   store.db.execute('DELETE FROM source_conflicts WHERE path=? AND record_id=?',(item['path'],key))
   meta=json.loads(store.db.execute('SELECT document FROM source_files WHERE path=?',(item['path'],)).fetchone()[0])
   if kind=='ambiguous':
    meta['ambiguous_rows']=[s for s in meta.get('ambiguous_rows',[]) if s!=item['source']]
    meta['ambiguous_details']=[d for d in meta.get('ambiguous_details',[]) if d['source']!=item['source']]
   meta['unresolved_conflicts']=store.db.execute('SELECT COUNT(*) FROM source_conflicts WHERE path=?',(item['path'],)).fetchone()[0]+len(meta.get('ambiguous_rows',[]))
   store.db.execute('UPDATE source_files SET document=? WHERE path=?',(json.dumps(meta,ensure_ascii=False),item['path']))
 store.export()
 return key
