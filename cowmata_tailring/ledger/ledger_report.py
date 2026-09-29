"""Single-day, inclusive-range and all-date ledger summaries."""
import re
from collections import Counter
from datetime import date as CalendarDate
from html import escape
from .ledger_core import now
LABELS=["产犊","孕后期","产后","难产","死胎","其他"]
STATES=["有效","无效","待判定","不适用"]
def sample_type(row):
    purpose=row.get("监测目的","")
    if purpose=="死胎" or re.search(r"样本有效\s*[（(]\s*死胎\s*[）)]",row.get("样本评价","")):return "死胎"
    return {"产犊监测":"产犊","孕后期监测":"孕后期","产后监测":"产后","难产":"难产"}.get(purpose,"其他")
def validity(row,criterion="三项均有效"):
    fields=["九轴"] if criterion=="九轴有效" else ["九轴","脉搏","温度"]
    values=[row.get(f,"") for f in fields]
    if all(v=="有效" for v in values):return "有效"
    if "无效" in values:return "无效"
    if any(v not in ("有效","/") for v in values):return "待判定"
    return "不适用"
def record_date(row):
    try:return CalendarDate.fromisoformat(row.get("记录日期","")).isoformat()
    except (ValueError,TypeError):return None
def unique_rows(rows):return list({r["记录ID"]:r for r in rows if r.get("已删除","0")!="1"}.values())
def date_bounds(rows):
    dates=[record_date(r) for r in unique_rows(rows) if record_date(r)]
    return (min(dates),max(dates)) if dates else (None,None)
def latest_date(rows):return date_bounds(rows)[1] or CalendarDate.today().isoformat()
def metric(rows,criterion):
    states=Counter(validity(r,criterion) for r in rows)
    valid=[r for r in rows if validity(r,criterion)=="有效"]
    groups=Counter(sample_type(r) for r in valid)
    cows={(r.get("牧场",""),r.get("牛号","")) for r in rows if r.get("牛号","").strip()}
    valid_cows={(r.get("牧场",""),r.get("牛号","")) for r in valid if r.get("牛号","").strip()}
    return {"total":len(rows),"valid":len(valid),"states":dict(states),"groups":dict(groups),"cows":len(cows),"valid_cows":len(valid_cows),
            "issues":sum(bool(r.get("核对提示")) for r in rows),"pending":sum(bool(r.get("_dirty")) for r in rows),
            "rate":len(valid)/len(rows) if rows else 0}
def build_report(rows,mode="all",start=None,end=None,criterion="三项均有效"):
    if mode not in ("all","single","range","cumulative"):raise ValueError("统计范围无效")
    if criterion not in ("三项均有效","九轴有效"):raise ValueError("统计口径无效")
    rows=unique_rows(rows)
    if mode=="single":start=end=CalendarDate.fromisoformat(start).isoformat()
    if mode in ("range","cumulative"):
        end=CalendarDate.fromisoformat(end).isoformat()
        start=CalendarDate.fromisoformat(start).isoformat() if mode=="range" else None
        if start and start>end:raise ValueError("开始日期不能晚于结束日期")
    chosen=rows if mode=="all" else [r for r in rows if record_date(r) and (not start or record_date(r)>=start) and record_date(r)<=end]
    first,last=date_bounds(chosen)
    if mode=="all":period="全部日期"+(f"（{first} 至 {last}）" if first else "")
    elif mode=="single":period=start+" 单日"
    elif mode=="range":period=start+" 至 "+end
    else:period="截至"+end
    result=metric(chosen,criterion)
    result.update(mode=mode,period=period,start=first if mode=="all" else start,end=last if mode=="all" else end,date_count=len({record_date(r) for r in chosen if record_date(r)}),
                  missing_dates=sum(not record_date(r) for r in chosen),excluded_dates=sum(not record_date(r) for r in rows) if mode!="all" else 0,generated_at=now(),criterion=criterion)
    result["categories"]=[dict(name=name,**metric([r for r in chosen if sample_type(r)==name],criterion)) for name in LABELS]
    dates=sorted({record_date(r) or "日期待补充" for r in chosen})
    result["daily"]=[dict(date=date,**metric([r for r in chosen if (record_date(r) or "日期待补充")==date],criterion)) for date in dates]
    parts=[f"{name}有效样本{result['groups'].get(name,0)}条" for name in LABELS if name!="其他" or result["groups"].get(name)]
    result["summary"]=f"{period}，台账共{result['total']}条，覆盖{result['cows']}个牛号；{criterion}样本{result['valid']}条，有效率{result['rate']:.1%}。"+ "\n"+"，".join(parts)+"。"
    result["summary"]+=f"\n无效{result['states'].get('无效',0)}条，待判定{result['states'].get('待判定',0)}条，不适用{result['states'].get('不适用',0)}条；需核对{result['issues']}条。"
    result["text"]=text_report(result)
    result["html"]=html_report(result)
    return result
def text_report(r):
    lines=["COWMATA 台账统计报告",r["summary"],"","分类明细（条）","类别\t总数\t有效\t无效\t待判定\t不适用\t有效率"]
    for c in r["categories"]:
        lines.append("\t".join(map(str,[c["name"],c["total"],c["valid"],c["states"].get("无效",0),c["states"].get("待判定",0),c["states"].get("不适用",0),f"{c['rate']:.1%}"])))
    lines+=["","按日期明细（各类别列为有效样本数）","记录日期\t总数\t有效\t无效\t待判定\t不适用\t产犊\t孕后期\t产后\t难产\t死胎\t其他"]
    for d in r["daily"]:
        lines.append("\t".join(map(str,[d["date"],d["total"],d["valid"],d["states"].get("无效",0),d["states"].get("待判定",0),d["states"].get("不适用",0)]+[d["groups"].get(n,0) for n in LABELS])))
    lines+=["","统计说明：","有效口径："+r["criterion"]+"；有效率＝有效样本数÷所选范围全部台账数。",
            "按记录日期（佩戴开始日期）统计，日期范围包含开始和结束当天；牛号按牧场与牛号组合去重。",
            "难产、死胎单列，分类不交叉；异常提示可能与有效/无效分类重叠，不重复加到总数。",
            f"本机尚未同步：{r['pending']}条。"+("包含本地未上传的修改。" if r["pending"] else "所选条目均已同步。"),
            f"所选范围日期待补充：{r['missing_dates']}条；因日期缺失未纳入当前日期筛选：{r['excluded_dates']}条。",
            "生成时间："+r["generated_at"]]
    return "\n".join(lines)
def html_report(r):
    def table(headers,rows):
        return '<table width="100%" cellspacing="0" cellpadding="7"><tr>'+''.join("<th>"+escape(str(x))+"</th>" for x in headers)+"</tr>"+''.join("<tr>"+''.join("<td>"+escape(str(x))+"</td>" for x in row)+"</tr>" for row in rows)+"</table>"
    cats=[[c["name"],c["total"],c["valid"],c["states"].get("无效",0),c["states"].get("待判定",0),c["states"].get("不适用",0),f"{c['rate']:.1%}"] for c in r["categories"]]
    days=[[d["date"],d["total"],d["valid"],d["states"].get("无效",0),d["states"].get("待判定",0),d["states"].get("不适用",0)]+[d["groups"].get(n,0) for n in LABELS] for d in r["daily"]]
    css="body{font-family:'Microsoft YaHei UI',sans-serif;color:#20332a;background:white;margin:22px;font-size:13px}h1{font-size:23px;color:#315c35}h2{font-size:16px;margin-top:24px}th{background:#eaf3e9;color:#34553a}td{border-bottom:1px solid #e4ebe1}td,th{text-align:center}p{line-height:1.7}.muted{color:#667569} .summary{background:#f0f7ed;padding:15px} @media print{body{margin:0}thead{display:table-header-group}tr{page-break-inside:avoid}}"
    html='<html><head><meta charset="utf-8"><title>台账统计报告</title><style>'+css+'</style></head><body><h1>台账统计报告</h1><p class="muted">'+escape(r["period"])+'</p><div class="summary">'
    html+="<p><b>台账 "+str(r["total"])+" 条　　有效 "+str(r["valid"])+" 条　　有效率 "+f"{r['rate']:.1%}"+"　　牛号 "+str(r["cows"])+" 个</b></p>"
    html+="<p>"+escape(r["summary"].split("\n",1)[-1]).replace("\n","<br>")+"</p></div><h2>各类别数据与有效性</h2>"
    html+=table(["类别","台账总数","有效","无效","待判定","不适用","有效率"],cats)
    html+="<h2>按日期统计</h2><p class='muted'>类别列统计有效样本；同一条样本只计入一个类别。</p>"
    html+=table(["记录日期","总数","有效","无效","待判定","不适用"]+LABELS,days) if days else "<p>所选日期没有台账。</p>"
    notes=r["text"].split("统计说明：",1)[-1]
    html+="<h2>统计口径与数据状态</h2><p class='muted'>"+escape(notes).replace("\n","<br>")+"</p></body></html>"
    return html
def summarize(rows,date,cumulative=True,criterion="三项均有效"):
    # Compatibility for integrations using the original report entry point.
    return build_report(rows,"cumulative" if cumulative else "single",date,date,criterion)
