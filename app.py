"""HeatPlan: a task-led, evidence-grounded urban heat decision workspace."""
import os
import json
import hashlib
import time
import importlib
from pathlib import Path
from contextlib import nullcontext
from copy import deepcopy
import pandas as pd
import streamlit as st
from dotenv import dotenv_values
# Refresh changed modules before rebinding session objects. Streamlit retains
# old objects across reruns, including render helpers and Agent tool closures.
runtime_revision=[]
for module_name in ('config','connection','websearch','project_search','decision','analysis','task','report','agent','project_advice','interface'):
    module=importlib.import_module('heatplan.'+module_name)
    stamp=Path(module.__file__).stat().st_mtime_ns
    if getattr(module,'_heatplan_source_stamp',None)!=stamp:
        module=importlib.reload(module)
        module._heatplan_source_stamp=stamp
    runtime_revision.append((module_name,stamp))
from heatplan.config import ROOT, DB_PATH, FEATURES, SCENARIO
import heatplan.analysis as analysis_module
if not all(hasattr(analysis_module.Analysis,name) for name in ("search_streets","search_road_policy")):
    importlib.invalidate_caches()
    analysis_module=importlib.reload(analysis_module)
Analysis=analysis_module.Analysis
evidence_cards=analysis_module.evidence_cards
records=analysis_module.records
from heatplan.agent import make_agent, run_online, run_workflow, revise_current, update_existing_policy
from heatplan.report import markdown_report
from heatplan.connection import check_connection,ConnectionError
from heatplan.project_advice import context_for_road,discuss_projects
from heatplan.task import GOALS, RISKS, CONSTRAINTS, DIRECTIONS, make_task, task_prompt, task_matches_report, requires_selection, adopt_report_task
from heatplan.interface import CSS, STEPS, GOAL_LABELS, esc, location, entry, show_metrics, metric_display, chat_bubble, chat_thinking, area_profile, render_network

st.set_page_config(page_title="HeatPlan · 城市高温治理助手", page_icon="🌿", layout="wide", initial_sidebar_state="collapsed")
st.markdown(CSS, unsafe_allow_html=True)
if not DB_PATH.exists():
    st.error("研究数据库尚未建立。")
    st.stop()
ss = st.session_state
if "analysis" not in ss:
    ss.analysis = Analysis()
elif ss.analysis.__class__ is not Analysis:
    # Streamlit can rerun app.py while retaining a session object created by
    # an older import of heatplan.analysis. Rebind that object to the current
    # class so its existing database, keys, and report state remain intact.
    try:
        ss.analysis.__class__=Analysis
    except TypeError:
        refreshed=Analysis()
        refreshed.__dict__.update(getattr(ss.analysis,"__dict__",{}))
        ss.analysis=refreshed
for key, value in dict(report=None, previous_report=None, messages=[], agent_instance=None,
    agent_fingerprint="", online_turns=0, pending_action=None, pending_request="", result_revision=0,
    focus_id=None, inspected_id=None, last_error="", request_context="followup", workspace_step=STEPS[0],
    chat_page_open=False, street_search_results=[], street_search_term="", street_search_district="",
    street_search_choice=0, inspected_street_summary=None, session_web_search_api_key="",
    street_policy_search_key=None, street_policy_search_result=None, street_policy_search_error="",
    pending_external_sources=[],active_external_sources=[],project_context=None,project_chat_history=[],connection_notice='',api_prompt=False).items():
    if key not in ss: ss[key] = value
if ss.get('runtime_revision')!=runtime_revision:
    ss.agent_instance=None;ss.agent_fingerprint='';ss.online_turns=0
    ss.runtime_revision=runtime_revision
if ss.get("policy_search_filter_version") != 4:
    ss.street_policy_search_result=None
    ss.street_policy_search_key=None
    ss.pending_external_sources=[]
    ss.active_external_sources=[]
    for report_key in ("report","previous_report"):
        cached_report=ss.get(report_key)
        if isinstance(cached_report,dict):cached_report["external_sources"]=[]
    ss.street_search_results=[]
    ss.project_context=None;ss.project_chat_history=[]
    ss.policy_search_filter_version=4
analysis = ss.analysis
data_min, data_max = analysis.utci_range()
# Keep task controls when their step is not mounted.
defaults = dict(task_district="朝阳区", task_goal="综合治理诊断", task_risk=RISKS[0],
    task_constraint=CONSTRAINTS[0], task_count=5, task_custom=data_min, task_max_cover=100,
    task_direction=next(iter(DIRECTIONS)), session_api_key="", task_sentence="")
for key, value in defaults.items():
    ss[key] = ss.get(key, value)
if ss.get("task_sync"):
    done = ss.pop("task_sync")
    ss.task_district = done["filters"]["district"]
    ss.task_goal = done["goal_label"]
    ss.task_risk = done["risk"]
    ss.task_custom = max(data_min, done["filters"]["min_utci"])
    ss.task_constraint = done["constraint"]
    ss.task_count = max(1, done["filters"]["count"])
    ss.task_max_cover = done.get("advanced_max_cover", 100)
    if done.get("reference_direction"):
        ss.task_direction = next(k for k,v in DIRECTIONS.items() if v == done["reference_direction"])

# Coverage remains diagnostic evidence, not a preset task filter.
ui_goals = [goal for goal in GOALS if GOALS[goal] != "low_cover"]
if ss.task_goal not in ui_goals:
    ss.task_goal = "综合治理诊断"
    ss.task_max_cover = 100

env = dotenv_values(ROOT / ".env")
def config_value(name, default=""):
    if os.getenv(name): return os.getenv(name)
    try:
        value = st.secrets.get(name)
        if value: return str(value)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass
    return env.get(name) or default
server_key_enabled=config_value("HEATPLAN_SERVER_KEY_ENABLED","0").lower() in ("1","true","yes")
# A hosted deployment should require every visitor to provide their own key.
# A server-side key is accepted only when the operator explicitly opts in.
api_key = ss.session_api_key or (config_value("HEATPLAN_API_KEY") if server_key_enabled else "")
web_search_api_key = ss.session_web_search_api_key or config_value("HEATPLAN_WEB_SEARCH_API_KEY")
base_url = config_value("HEATPLAN_BASE_URL", "https://api.deepseek.com")
model_id = ss.get('session_model') or config_value("HEATPLAN_MODEL", "deepseek-flash")
analysis.web_search_api_key = web_search_api_key
analysis.deepseek_api_key = api_key
analysis.chat_model_id = model_id
os.environ['NO_PROXY']=','.join(filter(None,[os.getenv('NO_PROXY',''),'api.deepseek.com'])) if 'api.deepseek.com' not in os.getenv('NO_PROXY','') else os.getenv('NO_PROXY','')

def current_task():
    return make_task(ss.task_district, ss.task_goal, ss.task_risk, ss.task_constraint, ss.task_count,
                     data_min, ss.task_custom, ss.task_max_cover, DIRECTIONS[ss.task_direction])
def navigate(step):
    ss.workspace_step = STEPS[step]
    ss.chat_page_open = step == 4
def queue(action): ss.pending_action = action
def queue_revision(action):
    ss.task_constraint = CONSTRAINTS[1]
    ss.pending_action = action
def ask(text, context="followup"):
    ss.pending_request = text
    ss.request_context = context
    ss.pending_action = "project_chat" if ss.project_context and context=='followup' else "chat"
    if context=='new':ss.project_context=None;ss.project_chat_history=[]
def open_chat():
    ss.project_context=None;ss.project_chat_history=[]
    ss.chat_page_open = True
    ss.workspace_step = STEPS[4]
def open_policy_chat(prompt,sources,road,references=None):
    ss.pending_request=prompt
    ss.request_context="followup"
    ss.pending_action="project_chat"
    ss.project_context=context_for_road(analysis,road,sources,references)
    ss.project_chat_history=[]
    ss.pending_external_sources=deepcopy(sources)
    ss.active_external_sources=deepcopy(sources)
    ss.chat_page_open=True
def submit_sentence():
    if ss.task_sentence.strip(): ask(ss.task_sentence.strip(), "new")

def submit_management_task():
    ss.task_goal='综合治理诊断';ss.task_constraint=CONSTRAINTS[2]
    prompt=f'请分析{ss.task_district}的不同热环境问题，选出最多{ss.task_count}处代表位置，比较改善方向、相似道路和论文依据。'
    if ss.task_sentence.strip():prompt+='管理要求：'+ss.task_sentence.strip()
    ask(prompt,'new')
def prompt_api():
    ss.api_prompt=True
def start_management_task():
    if api_key: submit_management_task()
    else: prompt_api()
def open_ai_chat():
    if api_key: open_chat()
    else: prompt_api()
def pick_candidate():
    ss.focus_id = ss.selected_candidate
    ss.inspected_id = None

@st.dialog("连接与数据说明", width="large")
def settings_dialog():
    if ss.session_api_key:
        st.write("本浏览器会话已应用 DeepSeek 密钥，可用于对话和项目检索。")
    elif api_key and server_key_enabled:
        st.write("当前部署启用了服务端密钥。评委也可以粘贴自己的密钥，使用自己的额度。")
    else:
        st.write("粘贴你自己的 DeepSeek 密钥，启用对话和项目检索。密钥只保留在本次浏览器会话中。")
    with st.form('deepseek_connection_form'):
        key = st.text_input("DeepSeek API 密钥", type="password",placeholder='粘贴密钥后，点击下方按钮')
        apply_key=st.form_submit_button('应用并检查连接',type='primary',width='stretch')
    if apply_key:
        try:
            with st.spinner('正在检查 DeepSeek 连接…'):
                connected_model=check_connection(key.strip(),model_id)
            # Do not write a visitor's secret into the shared deployment.
            # Streamlit session_state is isolated per browser session.
            ss.session_api_key=key.strip();ss.session_model=connected_model
            ss.agent_instance=None;ss.connection_notice='DeepSeek 连接成功。密钥已应用，对话和项目检索均可使用。'
            st.rerun()
        except ConnectionError as exc:st.error(str(exc))
        except OSError:
            st.warning('连接已应用到当前会话，本地配置暂未保存。')
    with st.expander("其他检索服务（可选）"):
        st.caption("已配置 DeepSeek 时无需填写。")
        search_key=st.text_input("Tavily 网页检索密钥",type="password",key="tavily_key_input")
        if st.button("应用本次会话的检索密钥",key="apply_tavily_key",disabled=not search_key):
            ss.session_web_search_api_key=search_key
            st.rerun()
        st.markdown("[获取 Tavily 检索密钥](https://app.tavily.com/home)")
    st.markdown("### 数据说明")
    st.write(SCENARIO)
    st.write("地图沿真实路网、按相邻分析位置的中点划分着色区间。UTCI 是综合温度、辐射、湿度和风速的热舒适指标。八项形态指标对应局部 10 m 缓冲区。")
    st.caption("相似道路帮助比较不同树冠配置下的治理方向。")
    if st.button("清空讨论记录"):
        ss.messages=[];ss.agent_instance=None;ss.online_turns=0
        ss.project_context=None;ss.project_chat_history=[]
        st.rerun()
    with st.expander("固定规则对照与执行记录"):
        st.button("运行固定规则对照",key="baseline_start",on_click=queue,args=("baseline",),width="stretch")
        st.button("对照：保留地点，不新增树木",key="baseline_revise",disabled=not bool(ss.report and not requires_selection(current_task(),ss.report)),on_click=queue_revision,args=("baseline",),width="stretch")
        if ss.report:
            st.dataframe(pd.DataFrame([{"顺序":i+1,"工具":r["tool"],"耗时 ms":r["duration_ms"]}
                for i,r in enumerate(ss.report["trace"])]),hide_index=True,width="stretch")
            with st.expander("完整调用依据"): st.json(ss.report["trace"])
    if ss.pending_action:
        st.rerun(scope="app")

@st.dialog("需要 DeepSeek API", width="medium")
def api_required_dialog():
    st.warning("这个功能需要调用 DeepSeek。请先粘贴你自己的 API 密钥；只读地图、道路搜索和指标查看不需要密钥。")
    st.caption("密钥仅保存在当前浏览器会话，不会写入网站公共配置。")
    with st.form('required_deepseek_connection_form'):
        key=st.text_input("DeepSeek API 密钥",type="password",placeholder="粘贴密钥")
        apply_key=st.form_submit_button("应用并检查连接",type="primary",width="stretch")
    if apply_key:
        try:
            with st.spinner('正在检查 DeepSeek 连接…'):
                connected_model=check_connection(key.strip(),model_id)
            ss.session_api_key=key.strip();ss.session_model=connected_model
            ss.agent_instance=None;ss.connection_notice='DeepSeek 连接成功，可以开始 AI 分析。'
            st.rerun()
        except ConnectionError as exc:
            st.error(str(exc))

if ss.pop('api_prompt',False):
    api_required_dialog()

if ss.workspace_step == STEPS[4]:
    ss.chat_page_open = True
task = current_task()

if ss.pending_action:
    action=ss.pending_action;ss.pending_action=None
    request=ss.pending_request if action in ('chat','project_chat') else task_prompt(task,ss.report)
    is_new=action=="chat" and ss.request_context=="new"
    ss.pending_request="";ss.request_context="followup";ss.last_error=""
    display_request=request if action in ('chat','project_chat') else f"{task['filters']['district']}：{GOAL_LABELS[task['goal_label']]}；{task['constraint']}。"
    ss.messages.append({"role":"user","content":display_request})
    previous_analysis_report=deepcopy(analysis.last_report)
    previous_policy=analysis.no_new_trees
    analysis.turn_external_sources=deepcopy(ss.pending_external_sources) if action in ('chat','project_chat') else []
    ss.pending_external_sources=[]
    thinking_started=time.perf_counter()
    chat_pending=action in ('chat','project_chat') and ss.chat_page_open
    thinking_slot=None
    if chat_pending:
        st.markdown('<div class="hp-eyebrow">北京五环内 · 面向城市规划与绿化管理</div>',unsafe_allow_html=True)
        st.title("HeatPlan AI 对话")
        st.markdown('<div class="hp-chat-workspace"><div class="hp-chat-workspace-title">和 HeatPlan AI 对话</div><div class="hp-chat-workspace-subtitle">你的问题已经发出，HeatPlan 正在整理道路数据和论文证据。</div></div>',unsafe_allow_html=True)
        for message in ss.messages:chat_bubble(message["role"],message["content"],message.get("thinking_seconds"))
        chat_thinking()
        progress_context=nullcontext(None)
    else:
        thinking_slot=st.empty()
        thinking_slot.markdown('<div class="hp-thinking"><span class="hp-thinking-ring"></span><span class="hp-thinking-text">HeatPlan正在思考中</span><span class="hp-thinking-detail">正在读取道路、形态和论文证据…</span></div>',unsafe_allow_html=True)
        progress_context=st.status("HeatPlan正在思考中",expanded=True)
    try:
        with progress_context as progress:
            if not chat_pending:
                st.write("核对任务范围，读取已有热环境与形态数据。")
            if action=='project_chat':
                analysis.no_new_trees=update_existing_policy(request,analysis.no_new_trees)
                ss.project_context=context_for_road(analysis,ss.project_context['road'],ss.project_context['sources'],ss.project_context.get('internal_references'))
                answer=discuss_projects(api_key,model_id,ss.project_context,request,ss.project_chat_history)
                ss.project_chat_history.extend([{'role':'user','content':request},{'role':'assistant','content':answer}])
                result={'model_text':answer,'task_kind':'conversation','external_sources':ss.project_context['sources'],
                    'hotspots':(ss.report or {}).get('hotspots',{'candidates':[],'returned':0})}
            elif action=="baseline":
                analysis.turn_task=deepcopy(task)
                analysis.require_new_selection=requires_selection(task,ss.report)
                result=run_workflow(analysis,task["filters"],request) if analysis.require_new_selection else revise_current(analysis,task["no_new_trees"])
            else:
                fingerprint=hashlib.sha256((api_key+base_url+model_id).encode()).hexdigest()
                if ss.agent_instance is None or ss.agent_fingerprint!=fingerprint:
                    ss.agent_instance=make_agent(analysis,api_key,base_url,model_id)
                    ss.agent_fingerprint=fingerprint;ss.online_turns=0
                turn_defaults=ss.report["hotspots"]["filters"] if action=="chat" and ss.report and not is_new else task["filters"]
                analysis.allow_pure_chat = bool(action=="chat" and ss.chat_page_open)
                if is_new:
                    analysis.no_new_trees=task["no_new_trees"]
                    request += "\n这是新的治理任务，请以本轮区域和用户要求重新确定对象；不得直接沿用旧候选。"
                result=run_online(ss.agent_instance,analysis,request,turn_defaults,reset=ss.online_turns==0,task=None if action=="chat" else task)
            if result.get("external_sources"):
                ss.active_external_sources=deepcopy(result["external_sources"])
            if result.get('project_context'):
                ss.project_context=result['project_context']
                ss.project_chat_history=[{'role':'user','content':request},{'role':'assistant','content':result['model_text']}]
            if action=='chat':
                ss.online_turns+=1
            discussion_only=result.get("task_kind")=="conversation" or (not ss.report and result["task_kind"] in ("evidence","location_comparison") and not result["hotspots"]["candidates"])
            if not discussion_only:
                actual_task=adopt_report_task(result,task)
                result["task"]=actual_task;analysis.last_report=deepcopy(result)
                old_focus=ss.focus_id
                ss.previous_report=ss.report;ss.report=result;ss.result_revision+=1;ss.task_sync=actual_task
                ids=[p["point_id"] for p in result["hotspots"]["candidates"]]
                ss.focus_id=old_focus if old_focus in ids else (ids[0] if ids else None)
                ss.inspected_id=None
                if action=="chat" and ss.chat_page_open:
                    ss.workspace_step=STEPS[4]
                else:
                    ss.workspace_step=STEPS[2] if result["task_kind"] in ("strategy_revision","case_comparison") else STEPS[1]
            else:
                analysis.last_report=previous_analysis_report
                ss.workspace_step=STEPS[4] if action in ("chat","project_chat") and ss.chat_page_open else STEPS[1]
            summary=result.get("model_text") or f"已按固定规则完成分析，返回 {result.get('hotspots',{}).get('returned',0)} 处候选。可查看各处的措施比较与治理清单。"
            ss.messages.append({"role":"assistant","content":summary,"thinking_seconds":round(time.perf_counter()-thinking_started,1)})
            if progress is not None:
                progress.update(label="分析完成，已生成道路诊断与证据。",state="complete",expanded=False)
    except Exception as exc:
        analysis.last_report=previous_analysis_report;analysis.no_new_trees=previous_policy
        ss.agent_instance=None
        ss.last_error=str(exc) if isinstance(exc,ConnectionError) else f"本轮任务未完成（{type(exc).__name__}）。请重试；上一轮结果仍保留。"
        ss.messages.append({"role":"assistant","content":ss.last_error,"thinking_seconds":round(time.perf_counter()-thinking_started,1)})
    finally:
        if thinking_slot is not None:
            thinking_slot.empty()
    st.rerun()

aligned=task_matches_report(task,ss.report)
report=ss.report if aligned else None
candidates=report["hotspots"]["candidates"] if report else []
ids=[r["point_id"] for r in candidates]
if candidates and ss.focus_id not in ids: ss.focus_id=ids[0]
if candidates: ss.selected_candidate=ss.focus_id
evidence={e["id"]:e for e in evidence_cards()}
def evidence_text(eids): return "；".join(f"{evidence[i]['title']}（{i}）" for i in eids if i in evidence and i != "E07")
def evidence_export(source):
    result=deepcopy(source)
    for key in ("trace","model_text","explanation_status","explanation_issue"):
        result.pop(key,None)
    result["evidence"]=[item for item in result.get("evidence",[]) if item.get("id")!="E07"]
    def strip_internal_ids(value):
        if isinstance(value,dict):
            for key,item in list(value.items()):
                if key in ("evidence_ids","decision_evidence_ids") and isinstance(item,list):
                    value[key]=[e for e in item if e!="E07"]
                else: strip_internal_ids(item)
        elif isinstance(value,list):
            for item in value: strip_internal_ids(item)
    strip_internal_ids(result)
    return result

brand,settings=st.columns([9,1.2])
with brand: st.markdown('<div class="hp-brand"><span>H</span>HeatPlan <small> / 城市高温治理助手</small></div>',unsafe_allow_html=True)
with settings:
    if st.button("设置与说明",width="stretch"): settings_dialog()
home=ss.workspace_step==STEPS[0]
st.markdown('<div class="hp-eyebrow">北京五环内 · 面向城市规划与绿化管理</div>',unsafe_allow_html=True)
st.title("找出高温道路，比较改善方案" if home else ("HeatPlan AI 对话" if ss.workspace_step==STEPS[4] else f"{task['filters']['district']} · 高温治理方案"))
if home:
    st.markdown('<div class="hp-subtitle">AI 结合道路热环境、建筑与树冠条件，帮你比较补植遮阴、树冠养护和现场核查方向。</div>',unsafe_allow_html=True)
    st.markdown('<div class="hp-summary"><span>找到治理对象</span><span>看懂建议依据</span><span>带走核查清单</span></div>',unsafe_allow_html=True)
else:
    st.caption("从真实道路数据出发，逐处查看问题、比较措施并形成行动清单。" if ss.workspace_step!=STEPS[4] else "与已读入论文和道路数据的 HeatPlan AI 连续对话。")
st.radio("治理流程",STEPS,horizontal=True,key="workspace_step",label_visibility="collapsed")
if ss.last_error:st.error(ss.last_error)
if ss.connection_notice:
    st.success(ss.connection_notice)
    ss.connection_notice=''
if ss.report and not aligned:
    st.warning("任务条件已改变。请重新开始诊断；旧方案不作为当前任务结果。")

def focus_picker():
    st.selectbox("查看候选道路",ids,format_func=lambda i:location(next(r for r in candidates if r["point_id"]==i)),
                 key="selected_candidate",on_change=pick_candidate)
    return next(r for r in candidates if r["point_id"]==ss.focus_id)

def render_diagnosis(row):
    m=entry(report["morphology"],row["point_id"])
    st.markdown("### "+location(row))
    st.caption(f"{row['district']} · {m['problem_type_name']}")
    a,b=st.columns(2)
    a.metric("模拟 UTCI",f"{row['utci']:.2f} °C")
    b.metric("树冠覆盖率",f"{row['canopy_cover']:.1%}")
    st.markdown('<div class="hp-note"><small>建议先做什么</small><div class="hp-answer">'+esc(m["candidate_action"])+'</div></div>',unsafe_allow_html=True)
    st.write("**判断依据**")
    st.write(m["problem_diagnosis"])
    st.caption("优先级："+m["priority_label"])
    with st.expander("查看八项形态指标"):show_metrics(row)
    st.button("比较这处的措施与证据 →",key="to_evidence",on_click=navigate,args=(2,),width="stretch")

def inspected_location(pid):
    frame=analysis.query("SELECT * FROM points WHERE point_id=? AND data_usable",[int(pid)])
    if frame.empty:return
    row=records(frame)[0]
    st.markdown("### "+location(row))
    road_summary=ss.inspected_street_summary
    if road_summary and int(road_summary["street_id"])==int(row["street_id"]):
        st.caption(f"{road_summary['street_name']} · 整条道路汇总 · {road_summary['point_count']} 个分析位置")
        cols=st.columns(3)
        cols[0].metric("道路平均 UTCI",f"{road_summary['mean_utci']:.2f} °C",road_summary["mean_heat_grade"])
        cols[1].metric("道路最高 UTCI",f"{road_summary['max_utci']:.2f} °C",road_summary["max_heat_grade"])
        cols[2].metric("强热应激及以上",f"{road_summary['strong_heat_points']}/{road_summary['point_count']}")
        st.info(road_summary["governance_reason"])
        st.caption("下方指标对应这条道路热应激最高的分析位置。")
    else:
        st.caption("地图查看位置 · 可以交给 HeatPlan 分析")
    st.metric("模拟 UTCI",f"{row['utci']:.2f} °C")
    show_metrics(row)
    if road_summary and int(road_summary["street_id"])==int(row["street_id"]):
        prompt=(f"请分析{road_summary['district']}{road_summary['street_name']}（街段 S{road_summary['street_id']}）。"
            f"整条道路有{road_summary['point_count']}个分析位置，平均UTCI {road_summary['mean_utci']:.2f}°C，"
            f"最高UTCI {road_summary['max_utci']:.2f}°C，强热应激及以上位置{road_summary['strong_heat_points']}个。"
            f"请调用 select_location 分析热应激最高位置 P{pid}，说明该道路是否值得优先治理、主要形态问题和有证据支持的候选措施。")
        label="让 AI 分析这条街道"
    else:
        prompt=f"请只分析指定位置 P{pid}，调用 select_location 选定该位置，比较治理措施、匹配相似道路并查询论文依据。"
        label="让 AI 分析这处道路"
    st.button(label,key="analyze_map_location",disabled=not api_key,width="stretch",on_click=ask,args=(prompt,"new"))

def render_policy_sources(sources):
    if not sources:return
    for category in dict.fromkeys(s.get('category','改造案例') for s in sources):
        selected=[source for source in sources if source.get("category")==category]
        if not selected:continue
        st.markdown("##### "+category)
        for source in selected:
            number=next(i+1 for i,s in enumerate(sources) if s['url']==source['url'])
            st.link_button(f"[{number}] "+(source.get("title") or "打开政府网站资料"),source["url"],width="stretch")
            date=source.get("published_date") or "发布日期未标注"
            source_type=source.get("source_type","政策资料")
            project_year=source.get("project_year")
            timing=f" · 项目年份 {project_year}" if project_year else ""
            st.caption(f"{source.get('domain','政府网站')} · {source_type}{timing} · 发布/更新 {date}")
            st.caption(source.get('relation','道路相关资料')+' · '+source.get('matched_road',''))
            if source.get('measures'):st.write('**可参考的措施：**'+'、'.join(source['measures']))
            if source.get('nearby_road_distance_m') is not None:
                st.caption(f"关联道路距目标道路 {source['nearby_road_distance_m']:.0f} m")
            if source.get('project_stage'):st.caption(source['project_stage']+' · '+source.get('time_evidence',''))
            if source.get("snippet"):st.write(source["snippet"])

def render_policy_search_result(result):
    status=result.get("status")
    if status=="not_configured":
        st.info("请先在右上角“设置与说明”中应用 DeepSeek 密钥。")
    elif status=="unnamed":
        return
    elif status=="not_found":
        st.info("数据库中没有找到这条道路的名称记录。")
    elif status=="ambiguous":
        st.info("这个名称对应多条道路，请使用完整道路名称。")
        st.caption("、".join(result.get("matches",[])))
    elif status=="error":
        st.warning(result.get("message","网页检索暂时不可用。"))
    elif status=="no_results":
        st.info("暂未检索到符合项目年份和措施条件的公开案例。")
    elif status=="ok":
        render_policy_sources(result.get("results",[]))


def render_internal_references(references):
    for key,title in (('morphology','整体形态相似的空间'),('contrast','建筑相近、树冠不同的治理对照')):
        result=references.get(key,{})
        st.markdown('##### '+title)
        st.caption(f"符合匹配条件 {result.get('eligible_points',0):,} 个分析位置，展示 {result.get('returned',0)} 个参考。")
        matches=result.get('matches',[])
        if not matches:
            st.write('当前条件下没有匹配到参考空间。')
            continue
        summary=[]
        for row in matches:
            summary.append({'参考道路':row.get('street_name') or '未命名道路','位置':f"P{row['point_id']}",
                '行政区':row['district'],'标准化形态距离':round(row['morphology_distance'],3),
                '已有 UTCI (°C)':round(row['utci'],2),'参考−目标 (°C)':row['utci_difference_reference_minus_target']})
        st.dataframe(pd.DataFrame(summary),hide_index=True,width='stretch')
        with st.expander(title+' · 八项指标与匹配方法'):
            st.caption(result.get('matching_method',''))
            rows=[]
            for feature,(label,unit) in FEATURES.items():
                row={'指标':label,'单位':unit,'目标':result['target_metrics'][feature]}
                row.update({f"{r.get('street_name') or '参考'} · P{r['point_id']}":r[feature] for r in matches})
                rows.append(row)
            st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')

def show_report_policy_sources():
    sources=report.get("external_sources",[]) if report else []
    if sources:
        with st.expander("道路政策与工程项目检索资料"):
            render_policy_sources(sources)

if ss.workspace_step==STEPS[0]:
    left,right=st.columns([.95,1.3],gap="large")
    with left:
        with st.container(border=True):
            st.markdown("### 先告诉我，你想解决什么")
            st.selectbox("治理区域",list(analysis.districts().district)+["全部区域"],key="task_district")
            st.text_area("这次想重点解决什么？（可选）",key="task_sentence",height=115,
                placeholder="例如：希望改善步行遮阴，不能新增树木；也想结合附近更新项目一起做。")
            with st.expander("分析范围与数量"):
                st.selectbox("热应激范围",RISKS,key="task_risk")
                if ss.task_risk==RISKS[3]:
                    st.number_input("自定义 UTCI 下限（≥ °C）",min_value=data_min,max_value=60.,step=.1,key="task_custom")
                st.select_slider("希望获得的候选位置数",options=list(range(1,21)),key="task_count")
            st.caption('直接说明要求，HeatPlan会据此比较措施；也可在右侧搜索具体道路。')
            st.button("开始城市高温治理诊断",key="start_diagnosis",type="primary",width="stretch",on_click=start_management_task)
            st.button("和 HeatPlan AI 对话 →",key="open_chat_home",width="stretch",on_click=open_ai_chat)
            if not api_key:st.info("只读道路地图和道路名称搜索可以直接使用；点击 AI 功能时会提示粘贴自己的 DeepSeek 密钥。")
        st.markdown('<div class="hp-mini">你将得到：道路诊断 → 措施与证据对照 → 可下载的治理核查清单。</div>',unsafe_allow_html=True)
    with right:
        st.markdown("### 先看道路热环境")
        st.caption(f"输入{ss.task_district}的街道名称查看道路热环境；也可以在地图上点击道路区间。")
        with st.form("street_name_search",clear_on_submit=False):
            search_col,submit_col=st.columns([4,1])
            with search_col:
                search_name=st.text_input("街道名称",key="street_search_query",placeholder="输入道路名称，例如：建国路")
            with submit_col:
                st.markdown("<div style='height:28px'></div>",unsafe_allow_html=True)
                submitted=st.form_submit_button("搜索街道",width="stretch")
        if submitted:
            ss.street_search_term=search_name.strip()
            ss.street_search_district=ss.task_district
            ss.street_search_choice=0
            if ss.street_search_term:
                with st.spinner(f"正在查询{ss.street_search_term}的道路热环境…"):
                    ss.street_search_results=analysis.search_streets(ss.street_search_term,ss.task_district,limit=20)
            else:
                ss.street_search_results=[]
        search_results=ss.street_search_results if ss.street_search_district==ss.task_district else []
        if ss.street_search_results and not search_results:
            st.caption("治理区域已改变，请重新搜索街道名称。")
        elif ss.street_search_term and not search_results:
            if submitted:st.info(f"在{ss.task_district}没有找到“{ss.street_search_term}”的道路，请检查名称或选择其他区域。")
        if search_results:
            if len(search_results)==20:
                st.caption("当前显示前20条匹配道路；输入更完整的名称可缩小范围。")
            selected_idx=st.selectbox("匹配道路",range(len(search_results)),key="street_search_choice",
                format_func=lambda i:f"{search_results[i]['street_name']} · {search_results[i]['district']} · 均值 {search_results[i]['mean_utci']:.2f}°C")
            road=search_results[selected_idx]
            st.markdown(f"#### {esc(road['street_name'])} · {esc(road['district'])}")
            cols=st.columns(4)
            cols[0].metric("治理判断",road["governance_status"])
            cols[1].metric("道路平均 UTCI",f"{road['mean_utci']:.2f} °C",road["mean_heat_grade"])
            cols[2].metric("道路最高 UTCI",f"{road['max_utci']:.2f} °C",road["max_heat_grade"])
            cols[3].metric("强热应激及以上",f"{road['strong_heat_points']}/{road['point_count']}")
            st.markdown('<div class="hp-note"><small>道路分析位置汇总</small>'+esc(road["governance_reason"])+f" 共汇总 {road['point_count']} 个分析位置。</div>",unsafe_allow_html=True)
            if st.button("查看这条道路的最高热应激位置",key="inspect_searched_street",width="stretch"):
                ss.inspected_id=int(road["hottest_point_id"])
                ss.inspected_street_summary=road
                st.rerun()
            if road.get("street_name") and road["street_name"] not in ("未命名道路","未命名街段"):
                policy_key=(road["district"],int(road["street_id"]))
                with st.expander("寻找相似空间与改造案例",expanded=True):
                    st.caption("匹配八项形态指标与树冠配置对照，再检索北京、其他城市及国际街道改造经验。项目时间从2021年起，附近道路项目作为补充。")
                    if not api_key:
                        st.caption("在右上角应用 DeepSeek 密钥后，即可检索并讨论项目。")
                    if st.button("匹配参考案例，生成改善思路",key="search_road_policy",width="stretch",type="primary"):
                        if not api_key:
                            api_required_dialog()
                        else:
                            with st.spinner(f"正在匹配{road['street_name']}的形态对照与真实改造案例…"):
                                ss.street_policy_search_result=analysis.search_road_policy(road["street_name"],road["district"])
                            ss.street_policy_search_key=policy_key
                            sources=ss.street_policy_search_result.get('results',[])
                            if ss.street_policy_search_result.get('status') in ('ok','no_results'):
                                open_policy_chat(f"请结合{road['street_name']}的形态相似空间、树冠配置对照和真实改造案例，比较可借鉴的改善方向，并说明优先与不优先采用的理由。",sources,road,ss.street_policy_search_result.get('internal_references'))
                                st.rerun()
                    policy_result=ss.street_policy_search_result if ss.street_policy_search_key==policy_key else None
                    if policy_result:
                        render_internal_references(policy_result.get('internal_references',{}))
                        render_policy_search_result(policy_result)
                        with st.expander('本次检索范围'):
                            st.caption('内部空间匹配覆盖北京五环内；外部检索围绕'+policy_result.get('search_focus','道路改善')+'。')
                            for query in policy_result.get('queries',[]):st.write(query)
                            for near in policy_result.get('nearby_roads',[]):
                                st.write(f"补充检索：{near['street_name']} · 道路几何间距 {near['distance_m']:.0f} m")
                        if policy_result.get('results') and api_key:
                            if st.button('继续结合这些项目讨论',key='discuss_road_policy',width='stretch'):
                                open_policy_chat(f"请结合{road['street_name']}的空间对照与改造案例，比较不同治理方向的优先顺序。",policy_result['results'],road,policy_result.get('internal_references'))
                                st.rerun()
        with st.spinner(f"正在加载{ss.task_district}道路热环境…"):
            selected=render_network(analysis,ss.task_district,[],ss.inspected_id,ss.result_revision,context="home",compact=True)
        if selected is not None:
            ss.inspected_id=selected;ss.inspected_street_summary=None;st.rerun()
        if ss.inspected_id:
            with st.container(border=True):inspected_location(ss.inspected_id)
    st.caption("数据：北京五环内道路 · 极端高温周模拟 · UTCI表示人体热应激水平")

elif ss.workspace_step==STEPS[1]:
    show_report_policy_sources()
    if not report:
        st.info("先提出治理任务，系统将生成道路问题诊断。")
        st.button("去提出任务 →",on_click=navigate,args=(0,))
    elif not candidates:
        st.info("没有符合当前任务条件的候选位置。可以调整区域或范围后重新诊断。")
        st.button("调整治理任务",on_click=navigate,args=(0,))
    else:
        summary_cols=st.columns(4)
        summary_cols[0].metric("候选位置",len(candidates))
        summary_cols[1].metric("不同问题类型",len({r["problem_type"] for r in candidates}))
        summary_cols[2].metric("匹配参考案例",sum(len(entry(report["similar"],i)["matches"]) for i in ids))
        summary_cols[3].metric("措施约束","不新增树木" if report["policy"]["no_new_trees"] else "可评估绿化增补")
        st.caption("查看代表位置的局部条件；系统按问题类型、热应激与空间间距组织候选。")
        map_col,detail_col=st.columns([1.65,1],gap="large")
        with map_col:
            st.markdown("### 道路在哪里，问题有什么不同")
            with st.spinner(f"正在加载{task['filters']['district']}道路热环境…"):
                selected=render_network(analysis,task["filters"]["district"],candidates,ss.inspected_id or ss.focus_id,ss.result_revision)
            if selected is not None:
                if selected in ids:ss.focus_id=selected;ss.inspected_id=None
                else:ss.inspected_id=selected
                st.rerun()
            st.caption("着色沿源路网在相邻分析位置中点处分界。紫色为候选区间，橙色为当前查看区间。")
        with detail_col:
            with st.container(border=True):
                if ss.inspected_id:inspected_location(ss.inspected_id)
                else:render_diagnosis(focus_picker())
        with st.expander("查看全部候选与选择依据"):
            st.dataframe(pd.DataFrame([{"道路位置":location(r),"热环境问题":r["problem_type_name"],"UTCI °C":r["utci"],
                "改善方向":entry(report["morphology"],r["point_id"])["candidate_action"]} for r in candidates]),hide_index=True,width="stretch")
            st.caption(report["hotspots"]["selection_rule"])

elif ss.workspace_step==STEPS[2]:
    show_report_policy_sources()
    if not candidates:
        st.info("先完成道路诊断，再比较各处的治理措施。")
        st.button("去提出任务 →",on_click=navigate,args=(0,))
    else:
        row=focus_picker();pid=row["point_id"]
        m=entry(report["morphology"],pid);sim=entry(report["similar"],pid)
        st.markdown("### 哪种措施更适合这里？")
        st.caption(m["problem_diagnosis"])
        for offset in (0,2):
            for col,opt in zip(st.columns(2),m["options"][offset:offset+2]):
                with col:
                    with st.container(border=True):
                        status_class=("insufficient" if opt["status"]=="证据不足" else
                            ("excluded" if opt["status"]=="约束排除" else ("pending" if opt["status"]=="暂缓" else "")))
                        st.markdown(f'<span class="hp-status {status_class}">{esc(opt["status"])}</span>',unsafe_allow_html=True)
                        st.markdown("#### "+opt["name"])
                        if opt["status"]=="证据不足":
                            st.markdown('<div class="hp-insufficient"><strong>暂不推荐的原因</strong>'+esc(opt["reason"])+"</div>",unsafe_allow_html=True)
                            st.caption("如需重新评估该措施，可补充核查："+"；".join(opt["field_requirements"]))
                        else:
                            reason_label="推荐依据" if opt["status"] in ("优先评估","备选") else ("排除原因" if opt["status"]=="约束排除" else "暂缓原因")
                            st.markdown(f"**{reason_label}**")
                            st.write(opt["reason"])
                            st.caption("下一步："+"；".join(opt["field_requirements"]))
                        with st.expander("为什么这样建议"):
                            for eid in opt["evidence_ids"]:
                                if eid == "E07": continue
                                e=evidence[eid];st.write(f"**{e['title']}（{eid}）**");st.write(e["summary"])
        st.markdown("### 建筑条件相近的道路，树冠配置有什么不同？")
        st.caption(f"匹配到 {sim['eligible_points']:,} 个位置 / {sim['eligible_streets']:,} 个源街段，本轮展示 {len(sim['matches'])} 例。")
        if sim["matches"]:
            comparisons=[{"对象":"当前道路","道路位置":location(row),"UTCI °C":row["utci"],"树冠覆盖":f"{row['canopy_cover']:.1%}","冠层体积 m³":row["canopy_volume"],"形态距离":None,"参考−目标 °C":None}]
            comparisons += [{"对象":"参考道路","道路位置":location(r),"UTCI °C":r["utci"],"树冠覆盖":f"{r['canopy_cover']:.1%}",
                "冠层体积 m³":r["canopy_volume"],"形态距离":r["morphology_distance"],"参考−目标 °C":r["utci_difference_reference_minus_target"]} for r in sim["matches"]]
            st.dataframe(pd.DataFrame(comparisons),hide_index=True,width="stretch")
            with st.expander("展开八项指标与匹配条件"):
                ref=st.selectbox("参考位置",sim["matches"],format_func=location,key=f"reference_{ss.result_revision}_{pid}")
                st.dataframe(pd.DataFrame([{"指标":label,"单位":unit,"目标":row[k],"参考":ref[k],"参考−目标":ref[k]-row[k]} for k,(label,unit) in FEATURES.items()]),hide_index=True,width="stretch")
                st.write(sim["matching_method"])
        else:st.info("没有符合条件的参考案例，匹配条件未放宽。")
        st.caption("比较相似道路的树冠配置与UTCI，帮助选择治理方向。")
        with st.expander("八项形态指标与论文原文"):
            show_metrics(row)
            for eid in m["decision_evidence_ids"]:
                if eid == "E07": continue
                e=evidence[eid]
                st.markdown(f"**{e['title']}（{eid}）**");st.write(e["summary"])
                st.caption(e["section"]+" · "+", ".join(e["source_paragraphs"]))
                st.text(e["source_text"])
        action_cols=st.columns([1,1,1])
        action_cols[0].button("不能新增树木，重新比较",key="revise_online",disabled=not api_key,on_click=queue_revision,args=("online",),width="stretch")
        action_cols[1].button("寻找冠体积更大的参考",disabled=not api_key,on_click=ask,args=(f"只为 P{pid} 寻找建筑条件相似、冠体积更大的参考案例，保留其他候选和治理约束。",),width="stretch")
        action_cols[2].button("形成治理清单 →",type="primary",on_click=navigate,args=(3,),width="stretch")
        if report.get("strategy_changes"):
            st.markdown("#### 约束改变后，哪些措施发生了变化")
            st.dataframe(pd.DataFrame([{"位置":f"P{c['point_id']}","措施":c["option"],"此前":c["before"],"现在":c["after"]} for c in report["strategy_changes"]]),hide_index=True,width="stretch")

elif ss.workspace_step==STEPS[3]:
    show_report_policy_sources()
    if not report:
        st.info("完成分析后，这里会生成可下载的治理清单。")
        st.button("去提出任务 →",on_click=navigate,args=(0,))
    else:
        st.markdown("### 可以带去现场讨论的治理清单")
        st.caption(f"{report['hotspots']['filters']['district']} · {len(candidates)} 处候选 · "+("不新增树木" if report["policy"]["no_new_trees"] else "可评估绿化增补"))
        actions=[]
        for r in candidates:
            m=entry(report["morphology"],r["point_id"])
            actions.append({"道路位置":location(r),"发现的问题":m["problem_type_name"],"优先核查方向":m["candidate_action"],
                "证据依据":evidence_text(m["decision_evidence_ids"]),"下一步":"；".join(m["field_checks"])})
            with st.expander(location(r)+" · "+m["priority_label"]):
                st.write("**建议方向：** "+m["candidate_action"])
                st.write("**依据：** "+m["problem_diagnosis"])
                for check in m["field_checks"]:st.write("• "+check)
                st.caption("下一步："+"；".join(m["field_checks"]))
        if actions:st.dataframe(pd.DataFrame(actions),hide_index=True,width="stretch")
        else:st.info("没有符合当前任务条件的候选位置，报告将记录本次空结果。")
        a,b,c=st.columns(3)
        a.download_button("下载治理报告",markdown_report(report),file_name="HeatPlan_report.md",mime="text/markdown",width="stretch",type="primary")
        b.download_button("下载核查清单",pd.DataFrame(actions).to_csv(index=False).encode("utf-8-sig"),file_name="HeatPlan_actions.csv",mime="text/csv",width="stretch")
        c.download_button("下载核验证据",json.dumps(evidence_export(report),ensure_ascii=False,indent=2),file_name="HeatPlan_evidence.json",mime="application/json",width="stretch")
        with st.expander("数据情景与结果来源"):
            st.write(SCENARIO);st.caption(report["mode"]+" · "+report["generated_at"])
            st.download_button("下载候选原始指标",pd.DataFrame(candidates).to_csv(index=False).encode("utf-8-sig"),file_name="HeatPlan_candidates.csv",mime="text/csv")
        st.button("返回比较措施",on_click=navigate,args=(2,))

elif ss.workspace_step==STEPS[4]:
    st.markdown('<div class="hp-chat-workspace"><div class="hp-chat-workspace-title">和 HeatPlan AI 对话</div><div class="hp-chat-workspace-subtitle">这个 AI 已经读入 HeatPlan 的道路数据、城市形态指标和论文证据。你可以先问它能做什么，再让它分析具体道路或修改治理约束。</div></div>',unsafe_allow_html=True)
    if ss.project_context:
        with st.expander('目标道路与内部参考空间',expanded=False):
            st.write(ss.project_context['road']['street_name']+' · '+ss.project_context['diagnosis']['problem_type_name'])
            render_internal_references(ss.project_context.get('internal_references',{}))
    if ss.active_external_sources:
        with st.expander("本轮参考的道路政策与工程项目资料",expanded=False):
            render_policy_sources(ss.active_external_sources)
    if not ss.messages:
        chat_bubble("assistant", "你好，我是 HeatPlan 研究顾问。我可以帮你识别不同高温问题、比较补植遮阴与树冠养护方向、寻找相似道路案例，并把你的管理要求转成一份可执行的治理清单。你想先了解我的能力，还是直接分析一个区域？")
        st.markdown("**可以这样开始**")
        prompt_cols=st.columns(3)
        prompt_cols[0].button("你能帮我做什么？",key="chat_capabilities",on_click=(ask if api_key else prompt_api),args=(("请先用简洁中文告诉我，你能结合HeatPlan道路数据、八项形态指标和论文证据完成哪些工作。","followup") if api_key else ()),width="stretch")
        prompt_cols[1].button("分析朝阳区",key="chat_chaoyang",on_click=(ask if api_key else prompt_api),args=(("请分析朝阳区的不同高温问题类型，比较治理方向并寻找相似道路。","new") if api_key else ()),width="stretch")
        prompt_cols[2].button("为什么选这些道路？",key="chat_why",on_click=(ask if api_key else prompt_api),args=(("请解释当前方案为什么选择这些道路，并比较不同问题类型的治理方向。","followup") if api_key else ()),width="stretch")
    else:
        for message in ss.messages:chat_bubble(message["role"],message["content"],message.get("thinking_seconds"))
    if ss.project_context and ss.project_chat_history:
        discussion='\n\n'.join(('管理要求：' if m['role']=='user' else 'HeatPlan：')+m['content'] for m in ss.project_chat_history)
        discussion+='\n\n参考来源：\n'+'\n'.join(f"[{i+1}] {s['title']} — {s['url']}" for i,s in enumerate(ss.project_context['sources']))
        st.download_button('下载道路改善讨论与来源',discussion,file_name='HeatPlan_case_discussion.txt',mime='text/plain')
    if api_key:
        request=st.chat_input("问 HeatPlan：例如“请解释这几条道路的区别”",key="advisor_chat_input")
        if request:ask(request);st.rerun()
    else:
        st.button("连接 DeepSeek 后开始对话",key="open_chat_connection_prompt",on_click=prompt_api,type="primary")

if ss.messages and ss.workspace_step not in (STEPS[0],STEPS[4]):
    st.divider()
    st.markdown("### 和 AI 继续完善方案")
    st.caption("可以追问推荐依据、寻找参考案例，或追加实施约束。")
    if len(ss.messages)>2:
        with st.expander(f"此前的讨论 · {len(ss.messages)-2} 条"):
            for message in ss.messages[:-2]:chat_bubble(message["role"],message["content"],message.get("thinking_seconds"))
    for message in ss.messages[-2:]:chat_bubble(message["role"],message["content"],message.get("thinking_seconds"))
    if api_key and not (ss.report and not aligned):
        request=st.chat_input("例如：不能新增树木，请保留这些地点重新比较措施。")
        if request:ask(request);st.rerun()
    elif not api_key:
        st.button("连接 DeepSeek 后继续完善方案",key="open_followup_connection_prompt",on_click=prompt_api)

