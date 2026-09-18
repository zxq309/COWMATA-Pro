"""Cell-wise reconciliation. Only changes from the saved baseline are submitted."""
import json
class DeletedRecordConflict(ValueError):pass

def raw_cells(row):
    try:
        value=json.loads(row.get('原始单元格') or '{}')
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError):return {}

def merge_record(baseline,incoming,current,schema=None):
    from ledger_core import FIELDS,BUSINESS,classify_record,directory,issues
    fields=schema.fields if schema else FIELDS
    keys=(schema.business+['牧场','工作表','记录类型'] if schema else BUSINESS+['记录日期','牧场','现场标记'])+['已删除']
    result={key:str(current.get(key,'') or '') for key in fields}
    def value(row,key):
        v=str(row.get(key,'') or '').strip()
        return (v or '0') if key=='已删除' else v
    if value(current,'已删除')!=value(incoming,'已删除'):
        # A stale live draft must never resurrect a server deletion.
        if baseline is None or value(baseline,'已删除')!=value(current,'已删除'):
            raise DeletedRecordConflict('该记录的删除状态已变化，请核对后选择恢复或保留删除')
    changed=[];overwritten=[]
    for key in keys:
        new=value(incoming,key);old=value(current,key)
        is_changed=new!=value(baseline,key) if baseline is not None else bool(new and key!='已删除')
        if is_changed and new!=old:
            result[key]=new;changed.append(key)
            if baseline is not None and old!=value(baseline,key):overwritten.append(key)
    # Workbook extras are cells too; display/style metadata is not a business edit.
    raw=raw_cells(current);source_raw=raw_cells(incoming);base_raw=raw_cells(baseline or {})
    extras=dict(raw.get('__extra__',{}));new_extras=source_raw.get('__extra__',{});old_extras=base_raw.get('__extra__',{})
    for key in set(new_extras)|set(old_extras):
        new=str(new_extras.get(key,'') or '')
        if (new!=str(old_extras.get(key,'') or '') if baseline is not None else bool(new)) and new!=str(extras.get(key,'') or ''):
            extras[key]=new;changed.append(key)
    if incoming.get('来源'):
        result['来源']=incoming['来源']
        raw={**raw,**source_raw}
    if extras or '__extra__' in raw:
        raw['__extra__']=extras
        result['时间说明']='；'.join([raw.get('__base_time_note__','')]+[k+'='+str(v) for k,v in extras.items()]).strip('；')
    elif baseline is not None and incoming.get('时间说明','')!=baseline.get('时间说明',''):
        result['时间说明']=incoming.get('时间说明','')
    result['原始单元格']=json.dumps(raw,ensure_ascii=False,separators=(',',':')) if raw else current.get('原始单元格','')
    if changed:
        if schema:result=schema.normalize(result)
        else:
            result['数据分类']=classify_record(result)[0];result['归类目录']=directory(result)
        result['核对提示']='；'.join((schema.issues if schema else issues)(result))
        result['修改时间']=incoming.get('修改时间') or current.get('修改时间','')
    return result,changed,overwritten
