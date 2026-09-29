"""Conservative continuation matching for renamed daily workbooks."""
import re,json
from pathlib import Path

def family(value):
 name=str(value).split(' / ')[0].replace('\\','/').rsplit('/',1)[-1]
 stem=Path(name).stem.strip()
 stem=re.sub(r'[(（]\d+[)）]$','',stem).strip()
 return re.sub(r'(?:\d{4}年)?(?:\d{1,2}月)?\d{1,2}日$','',stem).strip().casefold()

def continuation(row,current,schema=None):
 def v(r,k):return str(r.get(k,'') or '').strip()
 def location(r):
  parts=v(r,'来源').split(' / ')
  return parts[1] if len(parts)>1 else v(r,'工作表')
 result=[]
 for old in current:
  try:
   if old.get('已删除')=='1' and json.loads(old.get('原始单元格') or '{}').get('__superseded_by__'):continue
  except (ValueError,TypeError,AttributeError):pass
  if not v(row,'来源') or not v(old,'来源') or family(row['来源'])!=family(old['来源']):continue
  if v(row,'牧场')!=v(old,'牧场') or location(row)!=location(old):continue
  if schema is None:
   if not v(row,'牛号') or not v(row,'序号') or any(v(row,k)!=v(old,k) for k in ('牛号','序号')):continue
   if any(v(row,k) and v(old,k) and v(row,k)!=v(old,k) for k in ('预产期','设备号','佩戴开始')):continue
   if all(v(row,k) and v(old,k) for k in ('设备号','佩戴开始')):continue
   result.append(old)
  elif schema.id=='equipment':
   if v(row,'工作表')!=v(old,'工作表') or v(row,'记录类型')!=v(old,'记录类型'):continue
   kind=v(row,'记录类型')
   if kind=='汇总':
    label='合计' if v(row,'日期')=='合计' else '目前库存' if v(row,'设备来源')=='目前库存' else ''
    other='合计' if v(old,'日期')=='合计' else '目前库存' if v(old,'设备来源')=='目前库存' else ''
    if label and label==other:result.append(old)
   elif kind=='设备批次' and v(row,'记录日期') and v(row,'记录日期')==v(old,'记录日期'):
    a=set(re.findall(r'[0-9A-F]{4,12}',v(row,'设备号').upper()));b=set(re.findall(r'[0-9A-F]{4,12}',v(old,'设备号').upper()))
    if (a and b and (a<=b or b<=a)) or (not a and not b and v(row,'设备来源') and v(row,'设备来源')==v(old,'设备来源')):result.append(old)
   elif kind=='库存/说明' and v(row,'记录日期') and v(row,'记录日期')==v(old,'记录日期'):
    a=tuple(v(row,k) for k in ('备注','设备去向'));b=tuple(v(old,k) for k in ('备注','设备去向'))
    if any(a) and a==b:result.append(old)
 return result
