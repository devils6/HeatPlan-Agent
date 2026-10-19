import os,re,json
from smolagents import tool,ToolCallingAgent,OpenAIModel
from heatplan.config import ROOT
from heatplan.report import collect_report
from copy import deepcopy
from heatplan.task import requires_selection

def validate_model_explanation(answer,report):
    """Keep exact numerical facts in deterministic tables, not LLM paraphrases.

    This is a limited output contract, not a general truth/causality detector.
    References are allowed but must belong to the verified report.
    """
    text=str(answer)
    checked_text=text
    for source in report.get("external_sources",[]):
        for field in ("title","url","published_date","project_year"):
            literal=str(source.get(field) or "")
            if literal:checked_text=checked_text.replace(literal,"")
    if len(checked_text)>850:raise ValueError("回答过长。用约400字解释本轮策略差异，精确表格已由网页生成；不要再复述整份报告。")
    allowed_points={str(p["point_id"]) for p in report["hotspots"]["candidates"]}
    allowed_streets={str(p["street_id"]) for p in report["hotspots"]["candidates"]}
    for sim in report["similar"].values():allowed_points.update(str(r["point_id"]) for r in sim["matches"])
    for sim in report["similar"].values():allowed_streets.update(str(r["street_id"]) for r in sim["matches"])
    for comparison in report.get("focused_results",[]):allowed_points.update(str(r["point_id"]) for r in comparison["points"])
    for comparison in report.get("focused_results",[]):allowed_streets.update(str(r["street_id"]) for r in comparison["points"])
    if any(i not in allowed_points for i in re.findall(r"(?<![A-Za-z0-9_])P(\d+)(?!\d)",checked_text)):raise ValueError("引用了本次核验记录之外的地点，请仅引用实际返回编号")
    if any(i not in allowed_streets for i in re.findall(r"(?<![A-Za-z0-9_])S(\d+)(?!\d)",checked_text)):raise ValueError("引用了本次核验记录之外的源街段编号")
    allowed_evidence={e["id"] for e in report["evidence"]}
    if any(e not in allowed_evidence for e in re.findall(r"E\d{2}",checked_text)):raise ValueError("请先检索对应论文证据，不能引用尚未核验的证据编号")
    stripped=re.sub(r"(?<![A-Za-z0-9_])(?:P\d+|S\d+|E0[1-8])(?![0-9])","",checked_text)
    if re.search(r"\d",stripped) or "强热应激" in stripped or re.search(r"[零〇一二三四五六七八九十百千万两点]+(?:个百分点|立方米|米|摄氏度)",stripped):
        raise ValueError("不要在解释中重写温度、分级、容差、差值或阈值数字。精确值由程序表格展示；地点用P编号、证据用E编号，其余使用简短文字比较策略。")
    if any(s in checked_text for s in ("差值为负","差值为正","有正有负","方向不一致","正负都有","未被形态指标解释","未模拟因素","升为","提升为","降为","降级为","升级为")):
        raise ValueError("只解释当前措施适用性与现场核查需求。不要复述差值符号，不要虚构模型残差或未模拟因素，不要自行判断措施升降级；精确差值与状态变化均由程序表格显示。")
    if any(s in checked_text for s in ("无遮阴型","没有遮阴","完全无遮阴","无阴影")):
        raise ValueError("局部树冠与建筑指标为零，不等于没有遮阴；缓冲区外物体与太阳时段未核验。只能说局部未识别树冠，需调查实际遮阴。")
    for sentence in re.split(r"[。；\n]",checked_text):
        coverage_claim=re.sub(r'不(?:保证|代表|意味着)(?:覆盖|覆盖率)相近','',sentence)
        if "覆盖相近" in coverage_claim or "覆盖率相近" in coverage_claim:
            raise ValueError("请依据表格中的覆盖率数值描述目标与参考道路的差异。")
        support=[str(i) for i,m in report["morphology"].items() if all(m["metrics"][k]["value"]>0 for k in ("canopy_cover","canopy_height","canopy_volume"))]
        if len(support)>1 and "优先" not in sentence and re.search(r"(?:只有|仅有|仅)\s*P\d+.{0,35}(?:树冠|养护).{0,20}(?:支持|依据|证据)",sentence):
            raise ValueError("现有树冠指标支持的地点包括"+"、".join("P"+i for i in support)+"。优先评估与有无证据不是同一个问题，不可称只有一个地点有树冠依据。")
        if ("只能靠" in sentence or "只能通过" in sentence) and ("降温" in sentence or "效应" in sentence):
            raise ValueError("不要将某种验证方法说成唯一途径。简述尚需独立干预效果验证；单纯前后对比也不必然证明因果。")
        if "混杂" in sentence and any(term in sentence for term in ("树种","土壤","人流","人为热","蒸腾","风")):
            raise ValueError("不能把模拟未包含的树种、土壤、人流等说成造成模拟样本差异的混杂来源。请写匹配无法保证干预可比性；这些现实因素只列为现场核查项。")
    return True

def verified_chat_summary(report):
    """Create a readable answer from the verified structured report.

    This is deliberately deterministic.  When a model's free-form explanation
    fails the narrow output contract, the user should still receive a useful
    discussion answer based on the same checked locations, strategy states,
    references and field checks instead of an opaque rejection message.
    """
    hot=report.get("hotspots",{})
    candidates=hot.get("candidates",[])
    morphology=report.get("morphology",{})
    # Keep the validation marker machine-readable without putting a rejection
    # notice into the conversation bubble shown to the user.
    lines=[]
    if not candidates:
        requested=[e for call in report.get('trace',[]) if call['tool']=='get_research_evidence'
            for e in call['result'].get('evidence',[])]
        if requested:
            seen=set()
            for e in requested:
                if e['id'] in seen:continue
                seen.add(e['id'])
                lines.append(f"{e['title']}：{e['summary']}（{e['id']}）")
        else:
            lines.append('已找到相关项目资料，可以结合这些来源继续比较道路改善方向。' if report.get('external_sources') else '当前范围没有返回候选地点。可以换一个区域，或直接搜索具体道路继续分析。')
    else:
        for row in candidates:
            pid=row["point_id"]
            m=morphology.get(pid) or morphology.get(str(pid)) or {}
            name=row.get('street_name') or f'分析位置P{pid}'
            options=m.get('options',[])
            preferred=next((o for o in options if o['status']=='优先评估'),None)
            unsupported=next((o for o in options if o['status'] in ('证据不足','约束排除')),None)
            lines.append(f"{name}（P{pid}）：{m.get('problem_diagnosis') or m.get('candidate_action','请查看措施比较')}。")
            if preferred:lines.append(f"优先考虑{preferred['name']}：{preferred['reason']}")
            if unsupported:lines.append(f"本轮不优先考虑{unsupported['name']}：{unsupported['reason']}")
    if report.get("policy",{}).get("no_new_trees"):
        lines.append("- 本轮已按“禁止新增树木”重新比较措施，保留原候选地点；不适用的补植方向已在措施表中标出，现有树冠保护、培育或进一步调查仍需逐点判断。")
    return "\n\n".join(lines)

CAPABILITY_ANSWER = """我是 HeatPlan 研究顾问，已经读入道路热环境、建筑与树冠形态指标，以及论文中的研究证据。

我可以帮你完成五类工作：

• 识别一个区域里不同类型的高温道路，说明每类道路的形态特征。
• 比较补植与连续遮阴、保护和培育现有树冠、建筑与树冠协同等治理方向。
• 用八项指标匹配形态相似空间，或比较建筑条件相近、树冠配置不同的道路及已有模拟 UTCI。
• 根据你的管理要求重新比较方案，例如只做绿化、不能新增树木，或指定某一处道路。
• 检索北京、其他城市及国际街道改造经验，把实际措施与目标道路问题联系起来，给出参考理由和来源链接。

你可以直接说区域、道路编号或治理要求，我会先整理任务，再调用对应的数据和论文证据。"""

def make_tools(analysis):
    @tool
    def find_hotspots(district:str,min_utci:float|None=None,count:int|None=None,existing_canopy_only:bool|None=None,max_canopy_cover:float|None=None,min_canopy_cover:float|None=None)->dict:
        """从真实模拟数据中识别不同高温问题类型，并返回每类代表点、8项10m形态指标和治理方向。
        结果不是单纯按UTCI取前几名；会按透明规则覆盖低树冠、已有树冠但体积偏小、已有较高树冠仍高热和组合待核查等类型，再按UTCI和空间间距选代表点。
        Args:
            district: 区名，如朝阳区、海淀区、东城区、西城区、丰台区、大兴区、石景山区或全部区域。
            min_utci: UTCI筛选下限，省略时沿用本轮界面条件；初始为真实数据最小值。
            count: 候选数量，1至20；省略时沿用界面。
            existing_canopy_only: 用户明确要求重新筛选仅有树冠的地点时为true；不能新增树木本身只限制措施，不要求删除原地点。
            max_canopy_cover: 最大树冠覆盖率，0至1，默认不限制。
            min_canopy_cover: 最小覆盖率，0至1；仅养护模式还受用户界面最低覆盖率约束。
        """
        d=analysis.turn_defaults
        # Structured UI submissions bind scope exactly, including strict UTCI
        # boundaries. The Agent still decides whether and when to use tools.
        if getattr(analysis,"turn_task",None):
            return analysis.find_hotspots(**d)
        return analysis.find_hotspots(district,min_utci,count if count is not None else d.get("count",3),
            existing_canopy_only if existing_canopy_only is not None else d.get("existing_canopy_only",False),
            max_canopy_cover if max_canopy_cover is not None else d.get("max_canopy_cover",1.),
            min_canopy_cover if min_canopy_cover is not None else d.get("min_canopy_cover",0.))
    @tool
    def select_location(point_id:int)->dict:
        """用户明确从地图指定单个位置时选定真实分析对象，然后需补齐形态、参考案例与论文证据。
        Args:
            point_id: 用户明确指定的真实分析位置编号。
        """
        if getattr(analysis,'turn_task',None):
            raise ValueError('区域管理任务应调用find_hotspots；指定位置仅用于地图或自然语言明确选定对象。')
        return analysis.select_location(point_id)
    @tool
    def analyze_morphology(point_ids:list[int])->dict:
        """读取8项指标，为指定地点比较四种措施的适用性、约束排除与逐指标证据；亦用于保留原地点修订方案。
        Args:
            point_ids: 热点工具返回的真实point_id列表，最多20个。
        """
        return analysis.analyze_morphology(point_ids)
    @tool
    def find_similar_streets(point_id:int,count:int=3,direction:str="different",scope:str="district")->dict:
        """匹配建筑条件相似、树冠条件不同的真实参考点。
        Args:
            point_id: 热点工具返回的目标测点编号。
            count: 参考案例数，1至5。
            direction: higher_cover为覆盖至少高10个百分点，higher_volume为体积至少高100m³且覆盖最多低0.05，different为双向差异；按形态条件匹配。
            scope: district检索同区，city检索北京五环内全域。
        """
        return analysis.find_similar_streets(point_id,count,direction,scope)
    @tool
    def get_research_evidence(query:str,limit:int=4)->dict:
        """针对问题检索论文原有结论；E07仅在用户主动询问研究局限时查询。
        Args:
            query: 研究问题、关键词或证据编号，如E01 E04 E05 E06 E08。
            limit: 证据条数，1至8。
        """
        return analysis.get_research_evidence(query,limit)
    @tool
    def search_road_policy(street_name:str,district:str="朝阳区")->dict:
        """围绕已命名目标道路，同时检索八项指标形态相似空间、建筑相近树冠不同的治理对照，以及2021年以来北京、国内外真实改造案例。返回具体措施、项目状态、来源与空间对照。附近1公里道路项目作为补充。无名道路不查项目。
        Args:
            street_name: 数据库中的中文道路名称，或可唯一匹配到数据库道路名称的关键词。
            district: 行政区名称，用于限定道路并组织检索查询。
        """
        return analysis.search_road_policy(street_name,district)
    @tool
    def compare_locations(point_ids:list[int])->dict:
        """比较两至五个真实地点的八项指标、当前措施适用性和入选状态，用于解释为什么选A而非B。
        Args:
            point_ids: 两至五个真实测点编号。
        """
        return analysis.compare_locations(point_ids)
    @tool
    def set_governance_constraint(no_new_trees:bool,keep_locations:bool=True)->dict:
        """根据用户明确表达设置措施约束；设置后须重新分析现有地点，不能只改报告文字。
        Args:
            no_new_trees: 用户禁止种植新树时true，明确允许新增时false；不要自行猜测限制。
            keep_locations: 是否保留现有候选地点。默认true；仅用户明确要求重新选点才false。
        """
        import time
        start=time.perf_counter();analysis.no_new_trees=no_new_trees
        analysis.preserve_targets=keep_locations
        return analysis._finish("set_governance_constraint",dict(no_new_trees=no_new_trees,keep_locations=keep_locations),
            {"no_new_trees":no_new_trees,"keep_locations":keep_locations,"next":"重新调用analyze_morphology比较受影响措施；未补齐时方案不能完成。"},start)
    return [find_hotspots,select_location,analyze_morphology,find_similar_streets,get_research_evidence,search_road_policy,compare_locations,set_governance_constraint]

def make_agent(analysis,api_key,base_url,model_id,model_override=None):
    if not api_key and model_override is None:raise ValueError("尚未配置DeepSeek API密钥")
    model=model_override or OpenAIModel(model_id=model_id,api_base=base_url,api_key=api_key,
             client_kwargs={"timeout":90.0,"max_retries":1},temperature=0.1,max_tokens=3000,
             extra_body={"thinking":{"type":"disabled"}})
    instructions=(ROOT/"knowledge/agent_rules.md").read_text(encoding="utf-8")
    def complete(answer,memory,agent):
        if getattr(analysis,"allow_pure_chat",False) and not analysis.calls[analysis.turn_start:]:
            return True
        try:
            report=collect_report(analysis,analysis.turn_start,"验证",analysis.turn_request,str(answer),commit=False)
            return True
        except ValueError as exc:
            raise ValueError(str(exc))
    agent=ToolCallingAgent(tools=make_tools(analysis),model=model,instructions=instructions,
        max_steps=14,max_tool_threads=1,verbosity_level=0,add_base_tools=False,final_answer_checks=[complete])
    agent.heatplan_mode="本地脚本模型编排验证（不是DeepSeek结果）" if model_override is not None else "在线 DeepSeek ToolCallingAgent"
    return agent

def update_existing_policy(text,current):
    if any(s in text for s in ("不能新增","不新增","不想新增","不希望继续新增","不希望新增","仅养护","只允许改善现有","只考虑保护","仅现有","不种新树","不能种树","不得新增","禁止新增","禁止补植","不补植","只维护","只养护")):return True
    if any(s in text for s in ("允许新增","可以补植","允许补植","可以新增")):return False
    return current

def run_online(agent,analysis,request,defaults,reset=False,task=None):
    analysis.turn+=1;start=len(analysis.calls)
    analysis.last_explanation_error=None
    analysis.turn_start=start;analysis.turn_request=request;analysis.turn_defaults=dict(defaults)
    analysis.existing_only=bool(defaults["existing_canopy_only"])
    analysis.turn_task=deepcopy(task)
    previous=analysis.last_report
    if task:
        analysis.no_new_trees=task["no_new_trees"]
        analysis.require_new_selection=requires_selection(task,previous)
        analysis.preserve_targets=bool(previous and not analysis.require_new_selection)
    else:
        analysis.no_new_trees=update_existing_policy(request,analysis.no_new_trees)
        old_filters=previous["hotspots"]["filters"] if previous else {}
        analysis.require_new_selection=bool(previous and any(old_filters.get(k)!=v for k,v in defaults.items())) or '这是新的治理任务' in request
        analysis.preserve_targets=bool(previous and not analysis.require_new_selection and update_existing_policy(request,False) and not any(s in request for s in ("重新选点","重新筛选","换一批","改为分析","更换行政区","改为海淀","改为东城")))
    context="本轮界面默认筛选条件："+json.dumps(defaults,ensure_ascii=False)+"。用户文字明确改变条件时按用户要求执行。措施约束："+json.dumps({"no_new_trees":analysis.no_new_trees},ensure_ascii=False)
    if task:
        context+="\n已提交管理任务："+json.dumps(task,ensure_ascii=False)+"。本轮find_hotspots自动绑定该任务精确范围，禁止自行放宽。"
        context+=("必须重新调用find_hotspots并为新候选补全形态、案例与论文证据。" if analysis.require_new_selection else "沿用原候选，重新analyze_morphology核对当前措施；不需重新find_hotspots。")
        if task.get("reference_direction"): context+="本轮每个候选的案例检索方向必须为"+task["reference_direction"]+"。"
    if previous:
        context+="\n已有核验方案（可复用，不冒充本轮重新筛选）："+json.dumps({"time":previous["generated_at"],"filters":previous["hotspots"]["filters"],
          "candidates":[{"point_id":p["point_id"],"problem_type":p["problem_type_name"]} for p in previous["hotspots"]["candidates"]]},ensure_ascii=False)
    context+="\n用户请求："+request
    if getattr(analysis,"allow_pure_chat",False) and re.search(r"你能|能结合|能做什么|哪些工作|能力",request):
        previous_hotspots=(previous or {}).get("hotspots",{"candidates":[],"returned":0})
        return {"generated_at":None,"mode":agent.heatplan_mode,"request":request,
                "model_text":CAPABILITY_ANSWER,"trace":[],"task_kind":"conversation",
                "hotspots":deepcopy(previous_hotspots)}
    answer=agent.run(context,reset=reset)
    search_calls=[c for c in analysis.calls[start:] if c['tool']=='search_road_policy' and c['result'].get('status') in ('ok','no_results')]
    # Project discussion uses the road's own context, including both internal
    # comparisons; it does not inherit a different road from a previous plan.
    if search_calls and not any(c['tool'] in ('find_hotspots','select_location','analyze_morphology') for c in analysis.calls[start:]):
        from heatplan.project_advice import context_for_road,discuss_projects
        found=search_calls[-1]['result']
        roads=analysis.search_streets(found['street_name'],found['district'],100)
        road=next(r for r in roads if r['street_name']==found['street_name'])
        case_context=context_for_road(analysis,road,found['results'],found.get('internal_references'))
        discussion=discuss_projects(analysis.deepseek_api_key,getattr(analysis,'chat_model_id','deepseek-flash'),case_context,request)
        return {'mode':agent.heatplan_mode,'request':request,'model_text':discussion,'trace':analysis.calls[start:],
            'task_kind':'conversation','hotspots':deepcopy((previous or {}).get('hotspots',{'candidates':[],'returned':0})),
            'external_sources':found['results'],'project_context':case_context}
    if not analysis.calls[start:]:
        previous_hotspots=(previous or {}).get("hotspots",{"candidates":[],"returned":0})
        return {"generated_at":None,"mode":agent.heatplan_mode,"request":request,
                "model_text":str(answer),"trace":[],"task_kind":"conversation",
                "hotspots":deepcopy(previous_hotspots)}
    report=collect_report(analysis,start,agent.heatplan_mode,request,str(answer),commit=False)
    explanation_status="accepted"
    try:
        validate_model_explanation(answer,report)
    except ValueError as exc:
        analysis.last_explanation_error=str(exc)
        analysis.rejected_explanation=str(answer)
        explanation_status="withheld"
        answer=verified_chat_summary(report)
    result=collect_report(analysis,start,agent.heatplan_mode,request,str(answer))
    result["explanation_status"]=explanation_status
    result["explanation_issue"]=analysis.last_explanation_error
    # No additional API request is used to repair prose. Keep only verified
    # structured results in the published plan; rejected text remains local.
    analysis.last_report["explanation_status"]=explanation_status
    analysis.last_report["explanation_issue"]=analysis.last_explanation_error
    return result

def run_workflow(analysis,defaults,request="真实数据固定流程演示"):
    analysis.turn+=1;start=len(analysis.calls);analysis.existing_only=defaults["existing_canopy_only"]
    analysis.preserve_targets=False
    task=getattr(analysis,"turn_task",None)
    analysis.no_new_trees=task["no_new_trees"] if task else update_existing_policy(request,False)
    analysis.turn_defaults=dict(defaults)
    hot=analysis.find_hotspots(**defaults)
    ids=[r["point_id"] for r in hot["candidates"]]
    if ids:
        analysis.analyze_morphology(ids)
        for row in hot["candidates"]:
            direction="higher_cover" if row["canopy_cover"]<.4 else ("higher_volume" if row["canopy_volume"]<500 else "different")
            if task and task.get("reference_direction"): direction=task["reference_direction"]
            analysis.find_similar_streets(row["point_id"],direction=direction)
    analysis.get_research_evidence("E01 E04 E05 E06 E08",5)
    return collect_report(analysis,start,"固定流程演示（未调用大模型）",request)

def revise_current(analysis,no_new_trees=True):
    """Local baseline: a real strategy revision while keeping the target set."""
    if not analysis.last_report:raise ValueError("请先生成候选方案")
    analysis.turn+=1;start=len(analysis.calls);analysis.no_new_trees=no_new_trees
    analysis.preserve_targets=True
    ids=[r["point_id"] for r in analysis.last_report["hotspots"]["candidates"]]
    if ids:analysis.analyze_morphology(ids)
    else:analysis.get_research_evidence("E01 E04 E05 E06 E08",5)
    task=getattr(analysis,"turn_task",None)
    from heatplan.task import task_prompt
    request=task_prompt(task,analysis.last_report) if task else ("保持原地点，禁止新增树木并重新比较措施" if no_new_trees else "保持原地点，允许评估新增并重新比较措施")
    return collect_report(analysis,start,"固定流程演示（未调用大模型）",request)
