"""Workbook schemas for the two additional ledger sheets.

Canonical field names and CSV columns stay fixed. Workbook headers are matched by
name (normalized, plus known aliases), so a field's original Excel layout keeps
importing when a header is re-bracketed, padded, renamed, moved or followed by
helper columns. Only a missing required column stops an import.
"""
from __future__ import annotations
import csv,io,json,re,uuid,hashlib,unicodedata
from datetime import datetime,date,time,timedelta
from pathlib import Path
from .ledger_core import META,now,sha,normalize_time
BIRTH=["生产日期","牛场登记生产时间","牛号","预产期","提前预产期天数","是否佩戴过尾环","是否处于有效监测范围","备注(最后一次佩戴时间）"]
WEARING=["日期","新佩戴牛号","设备编码","拆除时间(掉落）","佩戴时长（天）","设备去向","库存数量","备注"]
MANAGEMENT=["日期","设备来源","设备数量","设备号","故障数量","故障设备编码","故障率(%)","遗失设备","备注"]
COMMON=["记录日期","牧场","工作表","记录类型","核对提示","来源","原始单元格","时间说明","归类目录"]
def text(value):
    if value is None:return ""
    if isinstance(value,datetime):return value.strftime("%Y-%m-%d") if not(value.hour or value.minute or value.second) else value.isoformat(sep=" ")
    if isinstance(value,time):return value.isoformat()
    if isinstance(value,date):return value.isoformat()
    if isinstance(value,float) and value.is_integer():return str(int(value))
    return str(value)

# ---- header contract -------------------------------------------------------
_BRACKETS=str.maketrans({"【":"(","】":")","[":"(","]":")","〔":"(","〕":")","〈":"(","〉":")","<":"(",">":")"})
_INVISIBLE=re.compile(r"[\s\u00a0\u200b-\u200f\u2028\u2029\u2060\u3000\ufeff]+")
def canonical_header(value):
    """Header identity: NFKC (full/half width), unified brackets, no spaces/line breaks."""
    value=unicodedata.normalize("NFKC",text(value)).translate(_BRACKETS)
    return _INVISIBLE.sub("",value).rstrip(":").lower()
KIND_TITLES={"calving":"产犊登记","wearing":"佩戴台账","management":"设备管理"}
KIND_FIELDS={"calving":BIRTH,"wearing":WEARING,"management":MANAGEMENT}
REQUIRED={"calving":("生产日期","牛号"),"wearing":("日期","新佩戴牛号","设备编码"),"management":("日期","设备来源","设备数量")}
HEADER_ALIASES={
    "calving":{
        "生产日期":["产犊日期","分娩日期"],
        "牛场登记生产时间":["生产时间","产犊时间","登记生产时间","牛场登记产犊时间","分娩时间"],
        "牛号":["耳号","牛耳号","牛只编号"],
        "提前预产期天数":["提前天数","提前预产天数","提前预产期(天)","提前预产期天数(天)"],
        "是否佩戴过尾环":["是否佩戴尾环","佩戴过尾环"],
        "是否处于有效监测范围":["是否有效监测","是否在有效监测范围","有效监测范围"],
        "备注(最后一次佩戴时间）":["备注","备注(最后佩戴时间)","最后一次佩戴时间"],
    },
    "wearing":{
        "日期":["佩戴日期"],
        "新佩戴牛号":["佩戴牛号","新佩戴牛只","牛号","耳号","牛耳号"],
        "设备编码":["设备号","设备编号","尾环编码","尾环号","设备id"],
        "拆除时间(掉落）":["拆除时间","掉落时间","拆除/掉落时间","拆除(掉落)时间","拆除或掉落时间"],
        "佩戴时长（天）":["佩戴时长","佩戴天数","佩戴时长(天数)"],
        "设备去向":["去向"],
        "库存数量":["库存","库存数"],
        "备注":["说明","备注说明"],
    },
    "management":{
        "设备来源":["来源"],
        "设备数量":["数量","设备数"],
        "设备号":["设备编码","设备编号","设备号码"],
        "故障数量":["故障数"],
        "故障设备编码":["故障设备号","故障设备编号","故障编码"],
        "故障率(%)":["故障率","故障率%","故障率(百分比)"],
        "遗失设备":["丢失设备","遗失设备编码","丢失设备编码","遗失设备号","丢失设备号"],
        "备注":["说明","备注说明"],
    },
}
class SheetLayout:
    """Where each canonical field lives in one worksheet."""
    def __init__(self,kind,title,header_row,columns,extras,headers):
        self.kind=kind;self.title=title;self.header_row=header_row;self.columns=columns;self.extras=extras;self.headers=headers
    @property
    def fields(self):return KIND_FIELDS[self.kind]
    @property
    def missing(self):return [f for f in self.fields if f not in self.columns]
    @property
    def canonical_title(self):return KIND_TITLES[self.kind]
def map_headers(values,kind):
    """Map header cells {col:value} to canonical fields; exact names win over aliases."""
    fields=KIND_FIELDS[kind];exact={canonical_header(f):f for f in fields}
    alias={}
    for field,names in HEADER_ALIASES.get(kind,{}).items():
        for name in names:alias.setdefault(canonical_header(name),field)
    columns={};extras={}
    headed={c:canonical_header(v) for c,v in values.items() if canonical_header(v)}
    for c,h in sorted(headed.items()):
        f=exact.get(h)
        if f and f not in columns:columns[f]=c
    for c,h in sorted(headed.items()):
        if c in columns.values():continue
        f=alias.get(h)
        if f and f not in columns:columns[f]=c
        else:extras[c]=text(values[c]).strip()
    return columns,extras
def title_kind(title):
    t=canonical_header(title)
    if "设备管理" in t or t.endswith("管理"):return "management"
    if "佩戴" in t:return "wearing"
    if "产犊" in t or "生产登记" in t or "分娩" in t:return "calving"
    return None
def used_width(ws):
    """Right-most column holding a value; styled-but-empty columns (common in WPS) are ignored."""
    cells=getattr(ws,"_cells",None)
    if cells is None:return max(ws.max_column or 0,1)
    return max((c for (r,c),cell in cells.items() if cell.value is not None),default=1)
def locate_layout(ws,kinds=("calving","wearing","management"),scan=12):
    """Find the header row and field columns; None when no kind's required columns are present."""
    hint=title_kind(ws.title);best=None
    last=min(ws.max_row or 0,scan);width=used_width(ws)
    for r in range(1,last+1):
        values={c:ws.cell(r,c).value for c in range(1,width+1)}
        if not any(canonical_header(v) for v in values.values()):continue
        for kind in kinds:
            columns,extras=map_headers(values,kind)
            if not all(f in columns for f in REQUIRED[kind]):continue
            score=(len(columns),kind==hint,-r)
            if best is None or score>best[0]:best=(score,SheetLayout(kind,ws.title,r,columns,extras,{c:text(v).strip() for c,v in values.items() if v is not None}))
    return best[1] if best else None
def describe_mismatch(ws,kind,scan=12):
    """Human-readable diagnostics for a sheet whose required columns cannot be found."""
    width=used_width(ws);best=None
    for r in range(1,min(ws.max_row or 0,scan)+1):
        values={c:ws.cell(r,c).value for c in range(1,width+1)}
        columns,_=map_headers(values,kind)
        if best is None or len(columns)>len(best[1]):best=(r,columns,values)
    if not best:return ws.title+"表头不完全匹配：工作表为空"
    r,columns,values=best
    missing=[f for f in REQUIRED[kind] if f not in columns]
    found="、".join(text(v).strip() for c,v in sorted(values.items()) if text(v).strip()) or "空"
    return f"{ws.title}表头不完全匹配：缺少必需列 {'、'.join(missing)}（第{r}行实际表头：{found}）。请保留这些列名后重新导入"
def layout_for(ws,workbook_kind):
    kinds=("calving",) if workbook_kind=="calving" else ("wearing","management")
    return locate_layout(ws,kinds)
def parse_date(value,year=2026,epoch=None):
    if isinstance(value,datetime):return value.date().isoformat()
    if isinstance(value,date):return value.isoformat()
    if isinstance(value,(int,float)) and 30000<=value<=80000:
        base=epoch or datetime(1899,12,30)
        return (base+timedelta(days=float(value))).date().isoformat()
    value=str(value or "").strip()
    try:
        if len(value)>10:return datetime.fromisoformat(value).date().isoformat()
        return date.fromisoformat(value).isoformat()
    except ValueError:pass
    m=re.fullmatch(r"(?:(\d{4})[年/.-])?(\d{1,2})[月/.-](\d{1,2})[日号]?",value)
    if m:
        try:return date(int(m[1] or year),int(m[2]),int(m[3])).isoformat()
        except ValueError:return ""
    return ""
def parse_removal(value,year=2026,epoch=None):
    if isinstance(value,datetime):return text(value)
    if isinstance(value,(int,float)) and 30000<=value<=80000:
        return text((epoch or datetime(1899,12,30))+timedelta(days=float(value)))
    raw=text(value).strip()
    if len(raw)>10:
        try:return text(datetime.fromisoformat(raw))
        except ValueError:pass
    basic=parse_date(value,year,epoch)
    if basic:return basic
    raw=text(value).strip();m=re.fullmatch(r"(\d{1,2})月(\d{1,2})日(\d{1,2})时(?:(\d{1,2})分?)?",raw)
    if m:
        try:return datetime(year,int(m[1]),int(m[2]),int(m[3]),int(m[4] or 0)).strftime("%Y-%m-%d %H:%M")
        except ValueError:pass
    return raw
def number(value):
    try:return float(value)
    except (TypeError,ValueError):return None
def device_list(value):return re.findall(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{4,12}(?![0-9A-Fa-f])",str(value or ""))
class SheetSchema:
    def __init__(self,id,title,filename,business,date_field,cow_field):
        self.id=id;self.title=title;self.filename=filename;self.business=business;self.fields=business+COMMON+META;self.date_field=date_field;self.cow_field=cow_field
        self.defaults={"sheet_id":id,"server_file":str(Path(r"F:\牛舍_现场记录")/filename)}
    def empty(self,settings=None,section=None):
        row=dict.fromkeys(self.fields,"");row.update({"记录ID":str(uuid.uuid4()),"已删除":"0","记录日期":datetime.now().strftime("%Y-%m-%d"),"牧场":(settings or {}).get("farm","扬大_高邮牧场"),"工作表":section or ("产犊登记" if self.id=="calving" else "佩戴台账"),"记录类型":"数据"})
        row[self.date_field]=row["记录日期"];return row
    def normalize(self,row):
        result={f:text(row.get(f,"")) for f in self.fields}
        result["记录日期"]=parse_date(result.get(self.date_field)) or result.get("记录日期","")
        result["已删除"]=result["已删除"] or "0";result["归类目录"]=self.directory(result)
        return result
    def directory(self,row):
        return "/".join([row.get("牧场",""),self.title,row.get("工作表",""),row.get("记录日期") or "日期待补充",row.get(self.cow_field) or row.get("记录类型","")])
    def fingerprint(self,row):
        values={f:text(row.get(f,"")) for f in self.business+["记录日期","牧场","工作表","记录类型","已删除"]}
        values["已删除"]=values["已删除"] or "0"
        return sha(json.dumps(values,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode())
    def identity(self,row):
        when=row.get("记录日期","");cow=row.get(self.cow_field,"")
        if self.id=="calving":parts=[row.get("牧场",""),when,cow]
        elif row.get("工作表")=="佩戴台账":parts=[row.get("牧场",""),when,cow,row.get("设备编码","").upper()]
        else:return ""
        if not when or not cow or not parts[-1] or parts[-1] in ("?","？"):return ""
        return sha(json.dumps([self.id]+parts,ensure_ascii=False).encode())
    def issues(self,row,required=True):
        errors=[]
        if row.get("记录类型") in ("日期分组","汇总"):return errors
        when=row.get("记录日期","")
        if not parse_date(when):errors.append("记录日期待核对")
        if self.id=="calving":
            if not row.get("牛号"):errors.append("牛号未填写")
            clock=row.get("牛场登记生产时间","")
            if not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?",clock):errors.append("牛场登记生产时间待核对")
            due=parse_date(row.get("预产期"));early=number(row.get("提前预产期天数"))
            if due and parse_date(when) and early is not None:
                actual=(date.fromisoformat(due)-date.fromisoformat(when)).days
                if early!=actual:errors.append(f"提前预产期天数原填{row['提前预产期天数']}，按日期计算应为{actual}，请核实")
            for field in ("是否佩戴过尾环","是否处于有效监测范围"):
                if row.get(field,"").strip() not in ("","是","否","/"):errors.append(field+"取值待核对")
        elif row.get("工作表")=="佩戴台账":
            if row.get("记录类型")=="佩戴":
                if not row.get("新佩戴牛号"):errors.append("新佩戴牛号未填写")
                device=row.get("设备编码","")
                if not re.fullmatch(r"[0-9A-Fa-f]{4}|[0-9A-Fa-f]{12}",device):errors.append("设备编码长度或字符待核对")
                removal=row.get("拆除时间(掉落）","")
                if removal in ("?","？"):errors.append("拆除时间(掉落）待核对")
                if removal and removal not in ("?","？","/"):
                    end=parse_date(removal)
                    if not end:errors.append("拆除时间(掉落）格式待核对")
                    elif when and end<when:errors.append("拆除时间(掉落）早于佩戴日期")
                    elif len(removal)==10 and number(row.get("佩戴时长（天）")) is not None and when:
                        days=(date.fromisoformat(end)-date.fromisoformat(when)).days
                        if number(row["佩戴时长（天）"])!=days:errors.append(f"佩戴时长（天）原填{row['佩戴时长（天）']}，两日期相差{days}天，需核对计时口径")
        elif row.get("工作表")=="设备管理":
            for count,devices in [("设备数量","设备号"),("故障数量","故障设备编码")]:
                n=number(row.get(count));codes=device_list(row.get(devices))
                if n is not None and codes and n!=len(codes):errors.append(f"{count}原填{row[count]}，{devices}列有{len(codes)}个编码，请核实")
                if any(len(c) not in (4,12) for c in codes):errors.append(devices+"存在非4位或12位编码")
            total=number(row.get("设备数量"));bad=number(row.get("故障数量"));rate=number(row.get("故障率(%)"))
            if total is not None and bad is not None:
                if bad>total:errors.append("故障数量大于设备数量")
                if total and rate is not None and abs(rate-bad/total*100)>0.0001:errors.append("故障率(%)与数量计算不符")
        return errors
    def csv_bytes(self,rows):
        stream=io.StringIO(newline="");writer=csv.DictWriter(stream,fieldnames=self.fields,lineterminator="\r\n");writer.writeheader()
        for row in sorted(rows,key=lambda r:(r.get("记录日期",""),r.get("工作表",""),r.get("来源",""),r.get("记录ID",""))):writer.writerow({f:row.get(f,"") for f in self.fields})
        return stream.getvalue().encode("utf-8-sig")
    def read_csv(self,data):
        reader=csv.DictReader(io.StringIO(data.decode("utf-8-sig"),newline=""),strict=True)
        if reader.fieldnames!=self.fields:raise ValueError("CSV表头不属于"+self.title)
        result=list(reader)
        if any(None in r or any(v is None for v in r.values()) for r in result):raise ValueError("CSV行列数不匹配")
        return result
SCHEMAS={
    "calving":SheetSchema("calving","产犊登记","扬大产犊登记汇总.csv",BIRTH,"生产日期","牛号"),
    "equipment":SheetSchema("equipment","设备台账","扬大测试设备台账.csv",list(dict.fromkeys(WEARING+MANAGEMENT)),"日期","新佩戴牛号")
}
def get_schema(id="samples"):
    if id=="samples":return None
    if id not in SCHEMAS:raise ValueError("未知台账Sheet")
    return SCHEMAS[id]
SAMPLE_KEYS=[canonical_header(v) for v in ("序号","牛号","预产期")]
def is_sample_sheet(ws,scan=20):
    return any([canonical_header(ws.cell(r,c).value) for c in (1,2,3)]==SAMPLE_KEYS for r in range(1,min(ws.max_row or 0,scan)+1))
def detect_workbook(path):
    import openpyxl
    wb=openpyxl.load_workbook(path,data_only=False)
    try:
        for ws in wb:
            if is_sample_sheet(ws):return "samples"
            layout=locate_layout(ws)
            if layout:return "calving" if layout.kind=="calving" else "equipment"
        hinted=[ws for ws in wb if title_kind(ws.title)]
        if hinted:raise ValueError(describe_mismatch(hinted[0],title_kind(hinted[0].title)))
    finally:wb.close()
    raise ValueError("表头与三张台账模板均不匹配，已停止导入")
def import_workbook(path,settings=None,notices=None):
    """Read one ledger workbook; compatibility decisions are appended to notices."""
    import openpyxl
    from openpyxl.utils import get_column_letter
    notices=[] if notices is None else notices
    id=detect_workbook(path)
    if id=="samples":
        from .ledger_import import import_excel
        return id,import_excel(path,settings)
    schema=SCHEMAS[id];wb=openpyxl.load_workbook(path,data_only=False);cached=openpyxl.load_workbook(path,data_only=True)
    try:
        layouts={};claimed={}
        ordered=sorted(wb.worksheets,key=lambda ws:0 if title_kind(ws.title) else 1)
        for ws in ordered:
            layout=layout_for(ws,id)
            hint=title_kind(ws.title)
            if layout is None:
                if hint in (("calving",) if id=="calving" else ("wearing","management")):raise ValueError(describe_mismatch(ws,hint))
                if any(c.value is not None for row in ws.iter_rows() for c in row):notices.append(f"工作表「{ws.title}」不是台账格式，已跳过（原文件未改动）")
                continue
            if layout.kind in claimed:
                notices.append(f"工作表「{ws.title}」与「{claimed[layout.kind]}」同为{layout.canonical_title}，已只导入后者");continue
            claimed[layout.kind]=ws.title
            if layout.missing:notices.append(f"{ws.title}缺少列：{'、'.join(layout.missing)}，按空白导入")
            renamed=[f"{layout.headers.get(c,'')}→{f}" for f,c in layout.columns.items() if layout.headers.get(c,'')!=f]
            if renamed:notices.append(f"{ws.title}表头已按标准列名识别：{'；'.join(renamed)}")
            if layout.header_row!=2:notices.append(f"{ws.title}表头位于第{layout.header_row}行，已自动识别")
            layouts[ws.title]=layout
        rows=[];year_candidates=[]
        for ws in wb:
            layout=layouts.get(ws.title)
            if not layout:continue
            date_cols=[layout.columns[f] for f in (("生产日期","预产期") if layout.kind=="calving" else ("日期",)) if f in layout.columns]
            for r in range(layout.header_row+1,ws.max_row+1):
                for c in date_cols:
                    value=ws.cell(r,c).value
                    if isinstance(value,datetime):year_candidates.append(value.year)
                    elif isinstance(value,(int,float)) and 30000<=value<=80000:year_candidates.append(int(parse_date(value,epoch=wb.epoch)[:4]))
        year=max(set(year_candidates),key=year_candidates.count) if year_candidates else None
        if not year:raise ValueError("原表缺少可确认年份，无法补齐日期；请在表内填写完整年份")
        for ws in wb:
            layout=layouts.get(ws.title)
            if not layout:continue
            section=layout.canonical_title;fields=layout.fields;mapped=set(layout.columns.values())
            extra_names={c:(layout.extras.get(c) or layout.headers.get(c) or "") for c in range(1,used_width(ws)+1) if c not in mapped}
            unmapped_rows=0
            merged={}
            for m in ws.merged_cells.ranges:
                if m.min_row<=layout.header_row:continue
                for r in range(m.min_row,m.max_row+1):
                    for c in range(m.min_col,m.max_col+1):merged[r,c]=(m.min_row,m.min_col)
            date_context="";date_origin=""
            date_letter=get_column_letter(layout.columns[schema.date_field if id=="calving" else "日期"])
            for r in range(layout.header_row+1,ws.max_row+1):
                physical=[ws.cell(r,c) for c in sorted(mapped)]
                extra_cells={c:ws.cell(r,c) for c in extra_names if ws.cell(r,c).value is not None}
                if not any(c.value is not None for c in physical):
                    if extra_cells:unmapped_rows+=1
                    continue
                row=schema.empty(settings,section="产犊登记" if id=="calving" else section);notes=[];raw={}
                for key in fields:
                    c=layout.columns.get(key)
                    if c is None:row[key]="";continue
                    origin=merged.get((r,c),(r,c));cell=ws.cell(*origin);original=ws.cell(r,c);value=cell.value
                    record={"field":key,"value":text(original.value),"type":type(original.value).__name__,"format":original.number_format,"resolved_from":cell.coordinate,"font_color":str(cell.font.color) if cell.font.color else "","fill_color":str(cell.fill.fgColor) if cell.fill.patternType else "","font_rgb":cell.font.color.rgb if cell.font.color and cell.font.color.type=="rgb" else "","comment":cell.comment.text if cell.comment else ""}
                    if cell.data_type=="f":
                        cached_value=cached[ws.title][cell.coordinate].value
                        record.update(formula=cell.value,cached=text(cached_value))
                        if cached_value is None:raise ValueError(f"{ws.title}!{cell.coordinate}公式没有缓存结果，请在Excel计算保存后导入")
                        value=cached_value
                    raw[original.coordinate]=record
                    row[key]=text(value)
                    if key in ("生产日期","预产期","日期"):
                        normalized=parse_date(value,year,wb.epoch)
                        if normalized:row[key]=normalized
                    if key=="拆除时间(掉落）":row[key]=parse_removal(value,year,wb.epoch)
                    if origin!=(r,c):notes.append(original.coordinate+"沿用合并单元格"+cell.coordinate)
                if extra_cells:
                    raw["__unmapped__"]={cell.coordinate:{"header":extra_names.get(c,""),"value":text(cell.value)} for c,cell in extra_cells.items()}
                current=parse_date(row[schema.date_field],year)
                if current:date_context=current;date_origin=f"{ws.title}!{date_letter}{r}"
                if id=="calving":
                    if not row["生产日期"]:
                        row["生产日期"]=date_context;notes.append("生产日期沿用"+date_origin)
                    row["记录类型"]="产犊"
                elif layout.kind=="wearing":
                    if not row["日期"]:row["日期"]=date_context;notes.append("日期沿用"+date_origin)
                    elif not current:notes.append("日期栏原文保留："+row["日期"]+"；日期归属沿用"+date_origin)
                    row["记录类型"]="佩戴" if row["设备编码"] else "库存/说明" if any(row.get(f) for f in ("库存数量","备注","设备去向")) else "日期分组"
                else:
                    row["记录类型"]="汇总" if row["日期"]=="合计" or row["设备来源"]=="目前库存" else "设备批次" if row["设备数量"] else "管理说明"
                row["记录日期"]=date_context if row["记录类型"]!="汇总" else ""
                row["来源"]=Path(path).name+" / "+ws.title+" / 第"+str(r)+"行"
                row["原始单元格"]=json.dumps(raw,ensure_ascii=False,separators=(",",":"))
                row["时间说明"]="；".join(notes)
                row["归类目录"]=schema.directory(row);row["核对提示"]="；".join(schema.issues(row))
                row["记录ID"]=str(uuid.uuid5(uuid.NAMESPACE_URL,id+":"+schema.fingerprint(row)))
                rows.append(row)
            used=sorted({c for c in extra_names for r in range(layout.header_row+1,ws.max_row+1) if ws.cell(r,c).value is not None})
            if used:notices.append(f"{ws.title}非标准列 "+"、".join(f"{get_column_letter(c)}列「{extra_names[c] or '无表头'}」" for c in used)+" 的原值已保留在原始单元格，不参与字段比对")
            if unmapped_rows:notices.append(f"{ws.title}有 {unmapped_rows} 行只在非标准列有内容，未作为台账记录")
    finally:wb.close();cached.close()
    return id,rows
