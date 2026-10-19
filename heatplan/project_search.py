"""Dated government project sources linked to real local road geometry."""
import re
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlparse, urldefrag
from heatplan.connection import api_json, ConnectionError
from heatplan.websearch import _search as tavily_search, _published_date, PolicySearchError

TOPICS=(('城市更新与老旧街区改造','城市更新 老旧小区 改造'),('生态廊道与道路绿化','绿化 绿道 生态廊道 道路改造'))
ACTION=r'开工|竣工|完工|建成|创建|启动|实施|改造|建设|施工|整治|提升|更新|招标|计划|completed|opened|launched|planted|installed|built|construction|project|pilot'
MEASURES={
    '行道树与树冠':r'行道树|乔木|补植|增植|树冠|树荫|street trees|tree canopy|tree planting',
    '连续遮阴与步行空间':r'遮阴|遮荫|遮阳|慢行|步行|人行道|shade|shading|pedestrian|sidewalk',
    '绿廊与绿地连接':r'绿道|绿廊|生态廊道|口袋公园|绿化带|greenway|green corridor|pocket park',
    '成熟树木与生长空间':r'保留.*树|保护.*树|树池|树穴|养护|成熟树|树木保护|mature trees|tree pits|tree preservation',
    '铺装与雨水管理':r'透水|海绵|雨水花园|凉爽路面|反照率|permeable|rain garden|cool pavement',
}


def official_source(host):
    return host.endswith(('.gov.cn','.gov','.gov.uk','.gov.au','.gov.sg','.gov.hk')) or host in ('paris.fr','www.paris.fr','barcelona.cat','www.barcelona.cat')


def measure_tags(text):
    return [name for name,pattern in MEASURES.items() if re.search(pattern,text,re.I)]


def clean(text):
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>',' ',str(text or '')))).strip()


def project_timing(text,published):
    """Use dated project actions, not arbitrary four-digit numbers or page dates."""
    events=[]
    text=re.sub(r'(?:发布日期|发布时间|信息时间|日期|时间|来源)\s*[:：]?\s*20\d{2}[-年/]\d{1,2}[-月/]\d{1,2}日?',' ',text)
    for sentence in re.split(r'[。；\n]|\[\.\.\.\]|(?<=[.!?])\s+',text):
        if not re.search(ACTION,sentence,re.I):continue
        for m in re.finditer(r'(?<!\d)(20\d{2})(?:\s*年|\b(?![-/]\d))',sentence):
            if re.match(r'\d{1,2}月\d{1,2}日',sentence[m.end():]) and not re.search(r'(?:于|自|至)\s*$',sentence[max(0,m.start()-3):m.start()]):continue
            lo=max(0,m.start()-65);hi=min(len(sentence),m.end()+95)
            excerpt=sentence[lo:hi]
            if re.search(ACTION,excerpt,re.I):events.append((int(m[1]),excerpt))
        if published and re.search(r'今年|年内|近日|日前|正在|近期',sentence):
            # Do not reinterpret a historical project as current just because
            # the same sentence contains a relative-time word.
            if not re.search(r'20\d{2}\s*年',sentence):events.append((published.year,sentence[:180]))
        if published and re.search(r'\b(?:this year|today|recently)\b',sentence,re.I) and not re.search(r'20\d{2}',sentence):
            events.append((published.year,sentence[:220]))
    recent=[(y,s) for y,s in events if 2021<=y<=date.today().year+10]
    if not recent:return None
    year,excerpt=max(recent,key=lambda x:x[0])
    planned=bool(re.search(r'计划|拟|招标|预计|将于|将|planned|will|expected',excerpt,re.I))
    stage='计划或招标' if planned else ('已完成' if re.search(r'竣工|完工|已建成|完成.*(?:改造|更新|建设)|已完成|completed|opened|installed',excerpt,re.I) else '实施动态')
    return {'project_year':year,'time_evidence':excerpt,'project_stage':stage}


def search_named_road(street_name,district,api_key='',max_results_per_topic=8,*,deepseek_api_key='',nearby_roads=None,reference_roads=None,problem_type='',no_new_trees=False):
    name=str(street_name or '').strip();area=str(district or '').strip()
    base={'street_name':name,'district':area,'results':[],'queries':[],'nearby_roads':nearby_roads or [],
          'radius_m':1000,'provider':'DeepSeek' if deepseek_api_key else 'Tavily'}
    if not name or name in ('未命名道路','未命名街段'):return dict(base,status='unnamed')
    if not (deepseek_api_key or api_key):return dict(base,status='not_configured')
    theme='保护成熟树木 树池改造 步行遮阴' if no_new_trees else ('树池改造 树木保护 养护 遮阴' if 'volume' in problem_type else ('遮阳 步行空间 绿道 更新' if 'high_canopy' in problem_type else '行道树 连续遮阴 步行空间 改造'))
    jobs=[('本道路相关项目',name,f'北京 {area} "{name}" {terms} 2021年以来 site:gov.cn','direct') for _,terms in TOPICS]
    for ref in (reference_roads or [])[:1]:
        jobs.append(('内部参考道路的项目',ref['street_name'],f'北京 "{ref["street_name"]}" 绿化 街道更新 改造 2021年以来 site:gov.cn','reference'))
    jobs.extend([
        ('其他城市与地区的改造案例','',f'北京 街道 {theme} 项目 建成 2021年以来 site:gov.cn','case'),
        ('其他城市的改造案例','',f'上海 街道 {theme} 建成 2023 site:gov.cn','case'),
        ('国际街道改善案例','','street heat shade tree planting project completed 2023 site:gov','case')])
    for near in (nearby_roads or [])[:1]:
        jobs.append(('附近道路的项目线索',near['street_name'],f'北京 {near["district"]} "{near["street_name"]}" 绿化 改造 工程 2021年以来 site:gov.cn','nearby'))
    base['nearby_roads']=(nearby_roads or [])[:1]
    base['search_focus']=theme
    def search(job):
        category,target,query,mode=job
        query=query[:128]
        if deepseek_api_key:
            result=api_json('v1/tools/search',deepseek_api_key,{'query':query,'count':max_results_per_topic,'need_content':True})
        else:result=tavily_search(query,api_key,max_results_per_topic)
        return category,target,query,mode,result.get('results',[])
    batches=[];errors=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures={pool.submit(search,job):job for job in jobs}
        for future in as_completed(futures):
            try:batches.append(future.result())
            except (ConnectionError,PolicySearchError) as exc:errors.append(str(exc))
    if not batches:raise PolicySearchError(errors[0] if errors else '检索暂时无法完成，请重试。')
    results={}
    near_by_name={r['street_name']:r for r in (nearby_roads or [])}
    for category,target,query,mode,items in sorted(batches,key=lambda b:(b[1]!=name,b[0],b[1])):
        base['queries'].append(query)
        for item in items:
            url=urldefrag(str(item.get('url') or '').strip())[0]
            host=(urlparse(url).hostname or '').lower().rstrip('.')
            if urlparse(url).scheme not in ('http','https') or not official_source(host):continue
            title=clean(item.get('title'))[:240]
            snippet=clean(item.get('summary') or item.get('content') or item.get('snippet'))[:4000]
            visible=title+'。'+snippet
            if target and target not in visible:continue
            tags=measure_tags(visible)
            if not tags:continue
            published=_published_date(item.get('date') or item.get('published_date'))
            if published is None:
                m=re.search(r'(?:发布时间|发布日期|信息时间|日期)\s*[:：]?\s*(20\d{2}[-/]\d{1,2}[-/]\d{1,2})',clean(item.get('content')))
                if m:published=_published_date(m[1].replace('/','-'))
            if published is None or published<date(2021,1,1):continue
            # Inspect relevant excerpts, not dates in unrelated page footers.
            content=clean(item.get('content'))
            related=[]
            anchor=re.escape(target) if target else '|'.join(MEASURES.values())
            for m in list(re.finditer(anchor,content,re.I))[:6]:related.append(content[max(0,m.start()-160):m.end()+300])
            timing=project_timing(visible+'。'+'。'.join(related),published)
            policy=bool(re.search(r'关于.*(?:意见|办法|通知|方案)|行动计划|专项规划|政策解读|条例',title))
            if timing is None and not policy:continue
            if mode=='case' and policy:continue
            relation='本道路名称关联' if name in visible else {'reference':'内部参考道路名称关联','nearby':'附近道路关联线索','case':'措施可借鉴','direct':'本道路名称关联'}[mode]
            nearby=near_by_name.get(target,{}) if mode=='nearby' else {}
            result={'category':category,'title':title,'url':url,'domain':host,'snippet':snippet[:1200],
                'published_date':published.isoformat(),'source_type':'政策资料' if policy else '改造项目',
                'project_year':None if policy else timing['project_year'],
                'relation':relation,'matched_road':name if relation=='本道路名称关联' else target,
                'measures':tags,'reference_reason':'资料涉及'+'、'.join(tags)+'，可与目标道路的改善方向对照。',
                'morphology_match':None,'source_quality':'政府原始公开资料',
                'nearby_road_distance_m':nearby.get('distance_m'),
                'project_distance_m':None,'score':float(item.get('rerank_score') or item.get('score') or 0)}
            if timing and not policy:result.update(timing)
            if url not in results or relation=='本道路名称关联':results[url]=result
    # Reserve space for each evidence category instead of filling the list
    # with many notices about the same target-road project.
    ranked=sorted(results.values(),key=lambda x:(x['relation']!='本道路名称关联',x['source_type']=='政策资料',-len(x['measures']),-x['score'],x['url']))
    counts={};chosen=[]
    for item in ranked:
        category=item['category']
        if counts.get(category,0)>=3:continue
        counts[category]=counts.get(category,0)+1;chosen.append(item)
    base['results']=chosen[:12]
    if errors:base['partial_error']='部分查询未完成，可重试补充。'
    return dict(base,status='ok' if base['results'] else 'no_results')
