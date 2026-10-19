import json
from copy import deepcopy
from datetime import datetime
from heatplan.config import SCENARIO,FEATURES
def collect_report(analysis,start_index,mode,request,model_text="",commit=True):
    all_calls=analysis.calls[start_index:]
    if not all_calls:raise ValueError("本轮未执行证据工具，不能标为核验完成")
    latest=next((i for i in range(len(all_calls)-1,-1,-1) if all_calls[i]["tool"] in ("find_hotspots","select_location")),None)
    calls=all_calls[latest:] if latest is not None else all_calls
    hot=next((c["result"] for c in reversed(calls) if c["tool"] in ("find_hotspots","select_location")),None)
    previous=analysis.last_report
    task=getattr(analysis,"turn_task",None)
    if getattr(analysis,"require_new_selection",False) and hot is None:
        raise ValueError("任务区域、范围或目标已改变，必须重新执行find_hotspots；不得提交旧候选")
    if task and analysis.no_new_trees!=task["no_new_trees"]:
        raise ValueError("措施约束与当前提交任务不一致")
    effective_hot=hot or (previous or {}).get("hotspots")
    if task and effective_hot and any(effective_hot["filters"].get(k)!=v for k,v in task["filters"].items()):
        raise ValueError("候选范围与当前提交任务不一致，不能自动放宽或保留旧筛选")
    base=deepcopy(previous) if hot is None and previous else {}
    names={c["tool"] for c in calls}
    kind="plan" if hot is not None else ("strategy_revision" if "analyze_morphology" in names else ("case_comparison" if "find_similar_streets" in names else ("location_comparison" if "compare_locations" in names else "evidence")))
    if hot is None:
        hot=base.get("hotspots")
        if hot is None:
            if kind not in ("evidence","location_comparison"):raise ValueError("尚无方案，请先识别候选或直接比较真实编号")
            f=dict(district="全部区域",min_utci=analysis.utci_range()[0],count=0,existing_canopy_only=False,max_canopy_cover=1.,min_canopy_cover=0.)
            hot={"filters":f,"matching_points":0,"matching_streets":0,"returned":0,"candidates":[],"problem_type_summary":[],"selection_rule":"本轮为针对性证据查询，未筛选候选地点"}
    hot=deepcopy(hot)
    morph=base.get("morphology",{});similar=base.get("similar",{});evidence={e["id"]:e for e in base.get("evidence",[])}
    morph.update({p["point_id"]:p for c in calls if c["tool"]=="analyze_morphology" for p in c["result"]["points"]})
    similar.update({c["result"]["target_point_id"]:c["result"] for c in calls if c["tool"]=="find_similar_streets"})
    evidence.update({e["id"]:e for c in calls if c["tool"]=="get_research_evidence" for e in c["result"]["evidence"]})
    ids=[r["point_id"] for r in hot["candidates"]]
    # A natural-language scope change is evidenced by a fresh tool query.
    # Preserve-location constraints apply within the unchanged scope only.
    if not task and previous and latest is not None and hot["filters"]!=previous["hotspots"]["filters"]:
        analysis.preserve_targets=False
    if previous and getattr(analysis,"preserve_targets",False) and set(ids)!={p["point_id"] for p in previous["hotspots"]["candidates"]}:
        raise ValueError("本轮应保留原地点修订措施，不能用重新选点代替策略修订")
    if any(i not in morph or i not in similar for i in ids):raise ValueError("候选点的形态或案例证据不完整；本轮不能标为完成")
    if task and task.get("reference_direction") and any(similar[i].get("direction")!=task["reference_direction"] for i in ids):
        raise ValueError("参考案例方向与当前任务不一致，请按指定方向重新检索")
    if ids and not {"E01","E04","E05","E06","E08"}<=set(evidence):raise ValueError("方案缺少情景、措施或热应激分级证据")
    if any(morph[i].get("no_new_trees") != bool(analysis.no_new_trees) for i in ids):
        raise ValueError("治理约束已改变，必须重新比较所有现有候选的措施，不能只修改文字")
    for row in hot["candidates"]:
        m=morph[row["point_id"]]
        for field in ("strategy_direction","problem_type","problem_type_name","problem_diagnosis"):row[field]=m[field]
    for typ in hot.get("problem_type_summary",[]):
        m=next((morph[i] for i in ids if morph[i]["problem_type"]==typ["problem_type"]),None)
        if m:typ["strategy_direction"]=m["candidate_action"]
        elif kind=="strategy_revision":typ["strategy_direction"]="本类型没有当前候选，尚未逐点评估本轮约束。"
    changes=[]
    if previous:
        old_m=previous["morphology"]
        for i in ids:
            if i not in old_m:continue
            old_options={o["code"]:o for o in old_m[i].get("options",[])}
            for opt in morph[i].get("options",[]):
                before=old_options.get(opt["code"],{}).get("status")
                if before and before!=opt["status"]:changes.append(dict(point_id=i,option=opt["name"],before=before,after=opt["status"],reason=opt["reason"]))
    now=datetime.now().isoformat(timespec="seconds")
    external_sources=[];seen_sources=set()
    for source in getattr(analysis,"turn_external_sources",[]) or []:
        if source.get("url") and source["url"] not in seen_sources:
            external_sources.append(deepcopy(source));seen_sources.add(source["url"])
    for call in all_calls:
        if call["tool"]!="search_road_policy":continue
        for source in call.get("result",{}).get("results",[]):
            if source.get("url") and source["url"] not in seen_sources:
                external_sources.append(deepcopy(source));seen_sources.add(source["url"])
    report={"generated_at":now,"mode":mode,"request":request,"scenario":SCENARIO,"hotspots":hot,"morphology":morph,"similar":similar,"evidence":list(evidence.values()),"model_text":model_text,"trace":all_calls,
            "task_kind":kind,"policy":{"no_new_trees":bool(analysis.no_new_trees),"building_changes":False},
            "strategy_changes":changes,"external_sources":external_sources,"reused_from":previous["generated_at"] if base else None,
            "selection_time":base.get("selection_time",base.get("generated_at")) if base else now,
            "focused_results":[c["result"] for c in calls if c["tool"]=="compare_locations"]}
    if task: report["task"]=deepcopy(task)
    if commit:analysis.last_report=deepcopy(report)
    return report

def markdown_report(report):
    hot=report["hotspots"];filters=hot["filters"]
    import math
    lower=filters["min_utci"]
    bound=("> 32" if lower==math.nextafter(32.,math.inf) else ("> 38" if lower==math.nextafter(38.,math.inf) else f"≥ {lower:g}"))
    lines=["# HeatPlan Agent 城市高温治理候选报告","",report["scenario"],"",
      f"生成时间：{report['generated_at']}","",f"运行方式：{report['mode']}",
      f"任务：{report['request']}","",
      f"范围：{filters['district']}；UTCI {bound} °C；树冠覆盖率 {filters.get('min_canopy_cover',0):.0%}–{filters['max_canopy_cover']:.0%}；仅现有树冠：{'是' if filters['existing_canopy_only'] else '否'}。",
      f"筛选命中 {hot['matching_points']:,} 个点、{hot['matching_streets']:,} 个街段，返回 {hot['returned']} 处候选。",
      "", "## 高温问题类型与治理方向", hot.get("classification_rule", "")]
    lines += ["",f"本轮任务：{report.get('task_kind','plan')}；禁止新增树木：{'是' if report.get('policy',{}).get('no_new_trees') else '否'}。"]
    if report.get("reused_from"):lines.append(f"沿用既有筛选对象和案例；本轮根据治理要求重新比较措施，调用{len(report['trace'])}次工具。")
    for change in report.get("strategy_changes",[]):
        lines.append(f"- P{change['point_id']}：{change['option']}，{change['before']}→{change['after']}。{change['reason']}")
    for typ in hot.get("problem_type_summary", []):
        lines.append(f"- **{typ['name']}**：匹配 {typ['matching_points']:,} 点，本轮代表点 {typ['selected']} 处；候选方向：{typ['strategy_direction']}")
    lines.extend([
      hot["selection_rule"],"",
      "|候选街段与测点|代表点 UTCI °C|街段模拟均值 °C|树冠覆盖率|冠层体积 m³|",
      "|---|---:|---:|---:|---:|"])
    for r in hot["candidates"]:
        name=(r["street_name"] or "未命名街段").replace("|","/")
        lines.append(f"|{name} / S{r['street_id']} / P{r['point_id']}|{r['utci']:.2f}|{r['street_mean_utci']:.2f}|{r['canopy_cover']:.1%}|{r['canopy_volume']:.1f}|")
    if not hot["candidates"]:lines.extend(["","没有满足条件的候选，阈值未自动放宽。"])
    for r in hot["candidates"]:
        i=r["point_id"];m=report["morphology"].get(i) or report["morphology"][str(i)]
        sim=report["similar"].get(i) or report["similar"][str(i)]
        lines.extend(["",f"## P{i} / 街段 S{r['street_id']}",m["morphology_note"],
          f"经纬度（WGS84）：{r['longitude']:.6f}, {r['latitude']:.6f}。",
          f"问题类型：{m.get('problem_type_name','未分类')}。诊断依据：{m.get('problem_diagnosis','')}",
          f"程序核验分级：{m.get('heat_stress_grade','未记录')}（E08）。",
          f"候选措施：{m['candidate_action']}（{', '.join(e for e in m.get('decision_evidence_ids',['E04','E05','E07']) if e != 'E07')}）","",
          "八项指标（代表点10m缓冲区）："])
        lines += ["",f"评估优先级：{m.get('priority_label','待评估')}。{m.get('priority_basis','')}",
                  "|备选措施|当前适用性|指标与约束理由|论文依据|", "|---|---|---|---|"]
        for o in m.get("options",[]):lines.append(f"|{o['name']}|{o['status']}|{o['reason']}|{', '.join(e for e in o['evidence_ids'] if e != 'E07')}|")
        for k,(label,unit) in FEATURES.items():lines.append(f"- {label}：{r[k]:.6g} {unit}")
        lines.extend(["",sim["matching_method"],sim["difference_definition"],
          f"符合匹配条件：{sim.get('eligible_points','未记录')}点 / {sim.get('eligible_streets','未记录')}街段；展示{len(sim['matches'])}例。"])
        for ref in sim["matches"]:
            lines.append(f"- 参考 P{ref['point_id']} / S{ref['street_id']}：UTCI {ref['utci']:.2f} °C，差值 {ref['utci_difference_reference_minus_target']:+.2f} °C，树冠覆盖 {ref['canopy_cover']:.1%}，形态距离 {ref['morphology_distance']:.3f}，间距 {ref['separation_m']:.0f} m。")
            lines.extend(["|指标|目标|参考|参考−目标|","|---|---:|---:|---:|"])
            for k,(label,unit) in FEATURES.items():lines.append(f"|{label} ({unit})|{r[k]:.6g}|{ref[k]:.6g}|{ref[k]-r[k]:+.6g}|")
        if not sim["matches"]:lines.append("没有符合条件的参考案例。")
        lines.extend(["","下一步："+"；".join(m["field_checks"])+"。"])
    lines.extend(["","## 论文证据"])
    for result in report.get("focused_results",[]):
        lines.append("地点对比："+result["limit"])
        for p in result["points"]:lines.append(f"- P{p['point_id']}：UTCI {p['utci']:.2f}°C；{p['priority_label']}；{p['strategy_direction']}")
    for e in report["evidence"]:
        if e["id"] == "E07": continue
        lines.append(f"- {e['id']} {e['title']}：{e['summary']} 来源：{e['section']}，{', '.join(e['source_paragraphs'])}。")
    if report.get("external_sources"):
        lines.extend(["","## 道路政策与工程项目检索"])
        for source in report["external_sources"]:
            date=f"（{source['published_date']}）" if source.get("published_date") else ""
            project_year=f" · 项目年份 {source['project_year']}" if source.get("project_year") else ""
            lines.append(f"- [{source.get('title') or '政府网站资料'}]({source['url']}) · {source.get('category','道路治理相关资料')} · {source.get('source_type','政策资料')}{project_year}{date}")
            if source.get("snippet"):lines.append(f"  - {source['snippet']}")
    return "\n".join(lines)
