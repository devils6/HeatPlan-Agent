"""Presentation helpers: real road network, compact evidence and visual hierarchy."""
import html
import re
import json
import math
import pandas as pd
import pydeck as pdk
import streamlit as st
from markdown_it import MarkdownIt
from pydeck.bindings.json_tools import default_serialize
from heatplan.analysis import Analysis
from heatplan.config import DB_PATH, FEATURES

STEPS = ['① 提出任务', '② 问题诊断', '③ 证据比较', '④ 决策报告', '⑤ AI 对话']
GOAL_LABELS = {'综合治理诊断':'找到优先核查的道路', '优先改善低树冠覆盖':'改善树冠覆盖不足的道路',
               '保护与改善现有树冠':'保护和改善现有树冠', '相似街道对照':'寻找可借鉴的道路案例'}
CSS = '''<style>
:root {--hp-ink:#14392f;--hp-muted:#64776e;--hp-green:#17614c;--hp-line:#dbe5df;}
.block-container {max-width:1520px;padding:2rem 3rem 4rem;}
[data-testid="stHeader"] {background:transparent;}
[data-testid="stToolbar"] {display:none;}
#MainMenu,footer {display:none;}
h1 {font-size:2.6rem!important;letter-spacing:-.06rem;line-height:1.2!important;color:var(--hp-ink);padding:.2rem 0 .6rem!important;}
h2 {font-size:1.45rem!important;color:var(--hp-ink);}
h3 {font-size:1.12rem!important;color:var(--hp-ink);}
.hp-brand {font-weight:750;font-size:1.05rem;color:var(--hp-ink);letter-spacing:.02rem;}
.hp-brand span {display:inline-block;background:#e3eee7;border-radius:6px;padding:5px 9px;margin-right:9px;color:#17614c;}
.hp-eyebrow {font-size:.76rem;color:var(--hp-green);letter-spacing:.1rem;margin:10px 0 7px;}
.hp-subtitle {font-size:1.05rem;color:#526c5e;line-height:1.8;margin:0 0 1rem;}
.hp-summary {display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px;}
.hp-summary span {font-size:.8rem;padding:5px 12px;background:#eaf1ec;border-radius:20px;color:#456250;}
div[data-testid="stVerticalBlockBorderWrapper"] {border-radius:16px!important;}
div[data-testid="stMetric"] {padding:10px 15px;background:#fff;border:1px solid var(--hp-line);border-radius:12px;}
div[data-testid="stMetricValue"] {font-size:1.45rem;}
.hp-note {background:#edf3ec;border-left:3px solid #388062;padding:13px 16px;margin:8px 0 12px;border-radius:0 9px 9px 0;line-height:1.7;}
.hp-note small {color:#698171;display:block;margin-bottom:4px;}
.hp-answer {font-size:1.1rem;line-height:1.75;color:#1c4935;font-weight:600;}
.hp-status {display:inline-block;font-size:.76rem;border-radius:6px;padding:3px 8px;background:#eaf1ec;color:#36634f;margin:0 0 8px;}
.hp-status.excluded {background:#f9e9e5;color:#9c473a;}
.hp-status.pending {background:#f8efdc;color:#856828;}
.hp-status.insufficient {background:#eee9f7;color:#674a91;}
.hp-insufficient {background:#f6f2fa;border-left:4px solid #8a6ab4;border-radius:0 10px 10px 0;padding:12px 14px;margin:8px 0;color:#49375f;line-height:1.7;}
.hp-insufficient strong {display:block;margin-bottom:4px;color:#674a91;}
.hp-mini {color:var(--hp-muted);font-size:.8rem;line-height:1.7;}
.hp-chat-row {display:flex;width:100%;margin:.55rem 0;}
.hp-chat-row.user {justify-content:flex-end;}
.hp-chat-row.assistant {justify-content:flex-start;}
.hp-chat-row>div {max-width:82%;min-width:0;}
.hp-chat-bubble {padding:14px 18px;border:1px solid var(--hp-line);border-radius:18px;background:#fff;line-height:1.7;overflow-wrap:anywhere;}
.hp-chat-bubble p:last-child {margin-bottom:0;}
.hp-chat-row.user .hp-chat-bubble {background:#e5f1e8;}
.hp-chat-meta {color:#84968d;font-size:.74rem;margin:3px 8px 0;}
.hp-chat-workspace {background:#f7faf8;border:1px solid var(--hp-line);border-radius:18px;padding:18px 20px;margin:12px 0 16px;}
.hp-chat-workspace-title {font-size:1.18rem;font-weight:700;color:var(--hp-ink);margin-bottom:4px;}
.hp-chat-workspace-subtitle {color:var(--hp-muted);font-size:.9rem;line-height:1.7;}
.hp-thinking {display:flex;align-items:center;gap:11px;background:#edf6f0;border:1px solid #cfe3d5;border-radius:14px;padding:12px 16px;margin:8px 0 14px;color:#17614c;}
.hp-thinking-ring {width:17px;height:17px;border:3px solid #b8d8c2;border-top-color:#17614c;border-radius:50%;animation:hp-spin .8s linear infinite;flex:0 0 auto;}
.hp-thinking-text {font-weight:700;letter-spacing:.02em;}
.hp-thinking-detail {color:#668273;font-size:.84rem;}
.hp-chat-thinking {display:flex;align-items:center;gap:10px;max-width:82%;padding:12px 16px;border:1px solid #cfe3d5;border-radius:18px;background:#edf6f0;color:#17614c;line-height:1.6;}
@keyframes hp-spin {to {transform:rotate(360deg);}}
[data-testid="stDeckGlJsonChart"] {border-radius:12px;overflow:hidden;}
.st-key-workspace_step [role="radiogroup"] {display:flex;gap:8px;border-bottom:1px solid var(--hp-line);padding:4px 0 16px;margin:4px 0 4px;}
.st-key-workspace_step [role="radiogroup"] label {flex:1;justify-content:center;padding:10px 12px;border:1px solid var(--hp-line);background:#fff;border-radius:10px;}
.st-key-workspace_step [role="radiogroup"] label:has(input:checked) {background:#e5efe8;border-color:#338060;}
@media(max-width:850px) {.block-container {padding:1rem 1rem 4rem;} h1 {font-size:1.85rem!important;} .hp-chat-bubble {max-width:94%;} .st-key-workspace_step [role="radiogroup"] label {flex:1 0 40%;}}
</style>'''

def esc(value): return html.escape(str(value))
def location(row): return f"{row.get('street_name') or '未命名道路'} · P{row['point_id']}"
def entry(mapping, pid): return mapping.get(pid) or mapping.get(str(pid))
def metric_display(row, key):
    return f'{row[key]:.1%}' if key.endswith('_cover') else f'{row[key]:,.2f}'

def show_metrics(row):
    st.dataframe(pd.DataFrame([{'指标':label,'数值':metric_display(row,k),'单位':'%' if k.endswith('_cover') else unit}
        for k,(label,unit) in FEATURES.items()]),hide_index=True,width='stretch')

def chat_bubble(role, content, thinking_seconds=None):
    role = role if role in ('user','assistant') else 'assistant'
    content = str(content)
    if role=='assistant':content=re.sub(r'<!--.*?-->','',html.unescape(content),flags=re.S).strip()
    meta = ''
    if role == 'assistant' and thinking_seconds is not None:
        meta = f'<div class="hp-chat-meta">HeatPlan · 思考 {float(thinking_seconds):.1f} 秒</div>'
    body=MarkdownIt('commonmark',{'html':False,'breaks':True}).disable('image').render(content) if role=='assistant' else esc(content).replace('\n','<br>')
    st.markdown(f'<div class="hp-chat-row {role}"><div><div class="hp-chat-bubble">{body}</div>{meta}</div></div>',unsafe_allow_html=True)

def chat_thinking():
    st.markdown('<div class="hp-chat-row assistant"><div class="hp-chat-thinking"><span class="hp-thinking-ring"></span><span class="hp-thinking-text">HeatPlan正在思考中</span></div></div>',unsafe_allow_html=True)

class CompactDeck(pdk.Deck):
    def to_json(self): return json.dumps(self,default=default_serialize,separators=(',',':'))

@st.cache_data(show_spinner=False)
def area_profile(district, stamp):
    sql='SELECT count(*) n,count(DISTINCT street_id) roads,min(utci) lo,max(utci) hi,min(longitude) west,max(longitude) east,min(latitude) south,max(latitude) north FROM points WHERE data_usable'
    a=Analysis();args=[]
    if district!='全部区域':sql+=' AND district=?';args=[district]
    return a.query(sql,args).iloc[0].to_dict()

@st.cache_data(show_spinner=False)
def road_segments(district, stamp): return Analysis().road_heat_segments(district)

@st.cache_data(show_spinner=False)
def metric_values(district, feature, stamp):
    if feature not in FEATURES:raise ValueError('未知形态指标')
    sql=f'SELECT point_id,{feature} AS value FROM points WHERE data_usable'
    frame=Analysis().query(sql+(' AND district=?' if district!='全部区域' else ''),[district] if district!='全部区域' else [])
    return dict(zip(frame.point_id,frame.value)),float(frame.value.min()),float(frame.value.max())

def heat_color(value,lo,hi):
    t=max(0.,min(1.,(float(value)-lo)/max(hi-lo,1e-9)))
    return [round(a+(b-a)*t) for a,b in zip((45,166,112),(218,67,58))]+[225]

@st.cache_data(show_spinner=False)
def map_payload(district,feature,mode,stamp):
    raw=road_segments(district,stamp)
    values,lo,hi=metric_values(district,feature,stamp) if feature else ({},*Analysis().utci_range())
    items=[]
    for seg in raw:
        value=values.get(seg['point_id']) if feature else seg['utci']
        color=heat_color(value,lo,hi) if value is not None and mode!='本轮治理候选' else [176,185,180,110]
        items.append(dict(p=seg['path'],c=color,s=seg['street_id'],i=seg['point_id'],n=esc(seg.get('street_name') or '未命名道路'),
                          u=f"{seg['utci']:.2f} °C" if seg['utci'] is not None else '无关联数据',v=f'{value:.4g}' if value is not None else '缺测'))
    return items,lo,hi

def render_network(analysis,district,candidates,focus_id,revision,context='diagnosis',compact=False):
    mode='UTCI 热应激分布';feature=None
    if not compact:
        mode=st.radio('地图图层',['UTCI 热应激分布','八项城市形态指标','本轮治理候选'],horizontal=True,key='map_layer')
        if mode=='八项城市形态指标':feature=st.selectbox('形态指标',list(FEATURES),format_func=lambda k:FEATURES[k][0],key='map_feature')
    segments,lo,hi=map_payload(district,feature,mode,DB_PATH.stat().st_mtime)
    ids={r['point_id'] for r in candidates}
    highlights=[{**s,'c':[255,159,46,255] if s['i']==focus_id else [107,65,176,255]} for s in segments if s['i'] in ids or (focus_id is not None and s['i']==focus_id)]
    # A near-transparent wider path provides reliable pointer targets without
    # making the entire heat map thick. Both layers use the same real geometry.
    hit_data=[{k:v for k,v in s.items() if k!='c'} for s in segments if s['i'] is not None]
    layers=[pdk.Layer('PathLayer',id='road-hit-areas',data=hit_data,get_path='p',get_color=[255,255,255,1],get_width=7,width_units="'pixels'",miter_limit=2,pickable=True),
            pdk.Layer('PathLayer',id='research-network',data=segments,get_path='p',get_color='c',get_width=1.3,width_units="'pixels'",width_min_pixels=1,miter_limit=2,pickable=True)]
    if highlights:
        layers.extend([pdk.Layer('PathLayer',id='candidate-halo',data=highlights,get_path='p',get_color=[255,255,255,245],get_width=12,width_units="'pixels'",miter_limit=2,cap_rounded=True,pickable=False),
                       pdk.Layer('PathLayer',id='candidate-roads',data=highlights,get_path='p',get_color='c',get_width=8,width_units="'pixels'",miter_limit=2,cap_rounded=True,pickable=True,auto_highlight=True)])
    bounds=area_profile(district,DB_PATH.stat().st_mtime)
    lon=(bounds['west']+bounds['east'])/2
    def mercator_y(latitude):return math.log(math.tan(math.pi/4+math.radians(latitude)/2))
    y1,y2=mercator_y(bounds['south']),mercator_y(bounds['north'])
    lat=math.degrees(2*math.atan(math.exp((y1+y2)/2))-math.pi/2)
    dx=max((bounds['east']-bounds['west'])/360,1e-6);dy=max((y2-y1)/(2*math.pi),1e-6)
    zoom=min(math.log2(540/(512*dx)),math.log2((350 if compact else 420)/(512*dy)),14)
    if not compact and focus_id and st.toggle('放大查看所选区间',key='zoom_candidate'):
        row=analysis.query('SELECT longitude,latitude FROM points WHERE point_id=?',[int(focus_id)])
        if not row.empty:lon,lat,zoom=float(row.iloc[0].longitude),float(row.iloc[0].latitude),16
    tooltip='<b>{n}</b><br/>街段 S{s} · 分析位置 P{i}<br/>模拟 UTCI：{u}'+(f'<br/>{FEATURES[feature][0]}：{{v}} {FEATURES[feature][1]}' if feature else '')
    deck=CompactDeck(layers=layers,initial_view_state=pdk.ViewState(longitude=lon,latitude=lat,zoom=zoom),map_style='light',map_provider='carto',tooltip={'html':tooltip})
    key=f'network_{context}_{revision}_{district}_{mode}_{feature}'
    event=st.pydeck_chart(deck,key=key,on_select='rerun',selection_mode='single-object',height=430 if compact else 500,width='stretch')
    if mode!='本轮治理候选':
        unit=FEATURES[feature][1] if feature else '°C'
        st.markdown(f'<div style="height:7px;border-radius:6px;background:linear-gradient(90deg,#2da670,#da433a)"></div><div style="display:flex;justify-content:space-between;font-size:.76rem;margin:4px 0"><span>{lo:.3g} {unit}</span><span>{hi:.3g} {unit}</span></div>',unsafe_allow_html=True)
    st.caption(f'{district}全部 {len(segments):,} 个道路着色区间 · 绿色至红色连续渐变' if mode!='本轮治理候选' else f'{district}全部 {len(segments):,} 个道路着色区间 · 紫色为候选，橙色为当前查看位置')
    for objects in event.selection.objects.values():
        for obj in objects:
            pid=obj.get('i');token=(key,pid)
            if pid is not None and st.session_state.get('last_network_pick')!=token:
                st.session_state.last_network_pick=token
                return int(pid)
    return None
