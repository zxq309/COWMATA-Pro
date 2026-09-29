"""Editors and reports for original calving and equipment columns."""
from html import escape
from collections import Counter
from PySide6.QtWidgets import *
from .ledger_sheets import BIRTH,WEARING,MANAGEMENT,number,device_list,parse_date
from .ledger_report import date_bounds,record_date,unique_rows
from .ledger_core import now
def visible_fields(schema,section):
    return BIRTH if schema.id=="calving" else MANAGEMENT if section=="设备管理" else WEARING
class SheetRecordDialog(QDialog):
    def __init__(self,row,schema,parent=None):
        super().__init__(parent);self.row=dict(row);self.schema=schema;self.result_row=None;self.fields={}
        self.setWindowTitle(schema.title+" · 编辑记录");self.resize(900,650)
        layout=QVBoxLayout(self);scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();form=QFormLayout(body)
        for key in visible_fields(schema,row.get("工作表")):
            value=row.get(key,"")
            if key in ("是否佩戴过尾环","是否处于有效监测范围"):
                w=QComboBox();w.setEditable(True);w.addItems(list(dict.fromkeys(["","是","否",value])));w.setCurrentText(value)
            elif key in ("备注","备注(最后一次佩戴时间）","设备号","故障设备编码","遗失设备"):
                w=QPlainTextEdit(value);w.setMaximumHeight(95)
            else:
                w=QLineEdit(value)
                if key in ("生产日期","日期","预产期"):w.setPlaceholderText("YYYY-MM-DD；原表说明也可保留")
                if key=="牛场登记生产时间":w.setPlaceholderText("HH:mm:ss；未知可保留 ?")
            self.fields[key]=w;form.addRow(key,w)
        scroll.setWidget(body);layout.addWidget(scroll,1)
        self.notice=QLabel("空白和原表说明可以保留；可疑值仅提示，不要求补填后才能同步。");self.notice.setWordWrap(True);layout.addWidget(self.notice)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel);buttons.button(QDialogButtonBox.Save).setText("保存");buttons.button(QDialogButtonBox.Cancel).setText("取消");buttons.accepted.connect(self.save);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
    def save(self):
        row=dict(self.row)
        for key,w in self.fields.items():row[key]=w.currentText() if isinstance(w,QComboBox) else w.toPlainText() if isinstance(w,QPlainTextEdit) else w.text()
        row=self.schema.normalize(row);row["核对提示"]="；".join(self.schema.issues(row));self.result_row=row;self.accept()
def build_sheet_report(rows,schema,mode="all",start=None,end=None):
    from datetime import date
    rows=unique_rows(rows)
    if mode=="single":start=end=date.fromisoformat(start).isoformat()
    if mode=="range":
        start=date.fromisoformat(start).isoformat();end=date.fromisoformat(end).isoformat()
        if start>end:raise ValueError("开始日期不能晚于结束日期")
    chosen=rows if mode=="all" else [r for r in rows if record_date(r) and start<=record_date(r)<=end]
    first,last=date_bounds(chosen)
    period=("全部日期"+(f"（{first} 至 {last}）" if first else "")) if mode=="all" else start+" 单日" if mode=="single" else start+" 至 "+end
    issues=sum(bool(r.get("核对提示")) for r in chosen);pending=sum(bool(r.get("_dirty")) for r in chosen)
    sections=[];lines=[schema.title+"统计报告",period]
    if schema.id=="calving":
        yes=lambda r,k:r.get(k,"").strip()=="是"
        worn=sum(yes(r,"是否佩戴过尾环") for r in chosen);monitored=sum(yes(r,"是否处于有效监测范围") for r in chosen)
        blank=sum(not r.get("是否处于有效监测范围","").strip() for r in chosen)
        summary=f"{period}，产犊登记{len(chosen)}条，涉及{len({r['牛号'] for r in chosen if r['牛号']})}个牛号；曾佩戴尾环{worn}条，原表标记在有效监测范围{monitored}条，监测范围未填写{blank}条；需核对{issues}条。"
        stats=[["产犊登记",len(chosen)],["佩戴过尾环",worn],["在有效监测范围",monitored],["监测范围未填写",blank],["需核对",issues]]
        daily=[]
        for day in sorted({record_date(r) or "日期待补充" for r in chosen}):
            batch=[r for r in chosen if (record_date(r) or "日期待补充")==day]
            daily.append([day,len(batch),sum(yes(r,"是否佩戴过尾环") for r in batch),sum(yes(r,"是否处于有效监测范围") for r in batch),sum(bool(r.get("核对提示")) for r in batch)])
        sections=[("登记情况",["项目","条数"],stats),("按日期统计",["生产日期","登记","佩戴过尾环","有效监测范围","需核对"],daily)]
        note="本表的“有效监测范围”直接按原字段统计，与样本台账的“九轴、脉搏、温度三项均有效”不是同一指标。"
    else:
        wearing=[r for r in chosen if r["记录类型"]=="佩戴"];batches=[r for r in chosen if r["记录类型"]=="设备批次"]
        devices={r["设备编码"].strip().upper() for r in wearing if r["设备编码"]}
        total=sum(number(r.get("设备数量")) or 0 for r in batches);fault=sum(number(r.get("故障数量")) or 0 for r in batches)
        removed=sum(bool(parse_date(r.get("拆除时间(掉落）"))) for r in wearing);unknown=sum(r.get("拆除时间(掉落）","").strip() in ("?","？") for r in wearing)
        ongoing=sum(not r.get("拆除时间(掉落）","").strip() for r in wearing)
        summary=f"{period}，原表信息{len(chosen)}行，其中佩戴{len(wearing)}条、设备编码{len(devices)}个、设备批次{len(batches)}批；已填拆除日期{removed}条，未填拆除日期{ongoing}条，拆除时间未知{unknown}条。设备批次数量合计{total:g}台，故障数量合计{fault:g}台；需核对{issues}条。"
        kinds=Counter(r["记录类型"] for r in chosen);stats=[[k,v] for k,v in kinds.items()]
        batch_rows=[[r["日期"],r["设备来源"],r["设备数量"],r["故障数量"],r["故障率(%)"] or "未填写"] for r in batches]
        daily=[]
        for day in sorted({record_date(r) for r in chosen if record_date(r)}):
            batch=[r for r in chosen if record_date(r)==day]
            daily.append([day,sum(r["记录类型"]=="佩戴" for r in batch),sum(r["记录类型"]=="设备批次" for r in batch),sum(bool(r["核对提示"]) for r in batch)])
        sections=[("信息类别",["类别","行数"],stats),("设备批次",["日期","来源","设备数量","故障数量","原表故障率(%)"],batch_rows),("按日期统计",["日期","佩戴条目","设备批次","需核对"],daily)]
        note="数量按设备管理批次行计算，原表合计和库存行完整保留但不重复累加。4位短编码与12位完整编码按原文保存，未擅自推断对应关系；同一设备可有多次佩戴。"
        inventories=[r for r in chosen if r.get("库存数量")]
        totals=[r for r in chosen if r.get("设备来源")=="目前库存"]
        if inventories:
            newest=max(inventories,key=lambda r:(r.get("记录日期",""),int(__import__("re").search(r"第(\d+)行",r.get("来源","第0行"))[1])))
            note+=f"佩戴页最近库存原填{newest['库存数量']}台（{newest['记录日期']}）。"
        if totals:note+=f"管理页原表库存公式缓存为{totals[-1]['设备数量']}台；两处口径可能不同，需管理员核对，程序未覆盖原值。"
    lines+=[summary,""]
    html="<html><head><meta charset='utf-8'><style>body{font-family:'Microsoft YaHei UI';font-size:13px;color:#20332a;margin:20px}h1{font-size:23px;color:#315c35}h2{font-size:16px}table{border-collapse:collapse}th{background:#eaf3e9}td{border-bottom:1px solid #e4ebe1}td,th{text-align:left;padding:7px}p{line-height:1.7}</style></head><body><h1>"+escape(schema.title)+"统计报告</h1><p>"+escape(summary)+"</p>"
    for title,headers,data in sections:
        lines += [title,"\t".join(headers)]+["\t".join(map(str,r)) for r in data]+[""]
        html+="<h2>"+escape(title)+"</h2><table width='100%' cellspacing='0' cellpadding='7'><tr>"+''.join("<th>"+escape(h)+"</th>" for h in headers)+"</tr>"
        for row in data:html+="<tr>"+''.join("<td>"+escape(str(v)).replace("\n","<br>")+"</td>" for v in row)+"</tr>"
        html+="</table>"
    note+=f" 本机尚未同步{pending}条。生成时间：{now()}"
    lines+=["统计说明",note];html+="<h2>统计说明</h2><p>"+escape(note)+"</p></body></html>"
    return {"mode":mode,"period":period,"total":len(chosen),"date_count":len({record_date(r) for r in chosen if record_date(r)}),"summary":summary,"text":"\n".join(lines),"html":html,"issues":issues,"pending":pending}
