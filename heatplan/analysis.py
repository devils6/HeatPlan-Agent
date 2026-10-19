"""Constrained, read-only research tools. All numeric outputs come from DuckDB."""
import json,math,time,uuid,tempfile
from datetime import datetime,timezone
from functools import lru_cache
from pathlib import Path
import duckdb
from heatplan.config import ROOT,DB_PATH,FEATURES,SCENARIO
from heatplan.decision import diagnose,strategy_options,type_sql,TYPE_ORDER,TYPE_NAMES,CLASSIFICATION,heat_stress_grade
from heatplan.project_search import search_named_road,PolicySearchError

def records(frame):
    return json.loads(frame.to_json(orient="records",force_ascii=False,double_precision=12))
@lru_cache(maxsize=1)
def evidence_cards():
    return json.loads((ROOT/"knowledge/evidence.json").read_text(encoding="utf-8"))

class Analysis:
    def __init__(self,db_path=DB_PATH,log_dir=None):
        self.db_path=str(db_path);self.calls=[];self.turn=0
        self.session_id=uuid.uuid4().hex[:12]
        self.log_path=(log_dir or ROOT/"logs")/f"{self.session_id}.jsonl"
        self.existing_only=False;self.min_existing_cover=0.20
        self.no_new_trees=False;self.last_report=None;self.turn_defaults={};self.web_search_api_key="";self.deepseek_api_key=""
    def query(self,sql,params=None):
        with duckdb.connect(self.db_path,read_only=True) as con:
            return con.execute(sql,params or []).fetchdf()
    def _finish(self,name,args,result,start):
        call={"timestamp":datetime.now(timezone.utc).isoformat(),"session":self.session_id,
              "turn":self.turn,"tool":name,"arguments":args,"duration_ms":round((time.perf_counter()-start)*1000,1),"result":result}
        self.calls.append(call)
        try:
            self.log_path.parent.mkdir(parents=True,exist_ok=True)
            with self.log_path.open("a",encoding="utf-8") as f:f.write(json.dumps(call,ensure_ascii=False,allow_nan=False)+"\n")
        except PermissionError:
            # The read-only analysis workflow should remain usable when the
            # checkout is mounted without write permission for its log folder.
            fallback=Path(tempfile.gettempdir())/"HeatPlan-Agent"/"logs"
            fallback.mkdir(parents=True,exist_ok=True)
            self.log_path=fallback/f"{self.session_id}.jsonl"
            with self.log_path.open("a",encoding="utf-8") as f:f.write(json.dumps(call,ensure_ascii=False,allow_nan=False)+"\n")
        return result
    def districts(self):
        return self.query("SELECT district,count(*) n,avg(utci) mean_utci FROM points GROUP BY district ORDER BY n DESC")

    def search_streets(self, name, district="全部区域", limit=20):
        """Find named streets and summarize their full set of usable analysis locations."""
        term=str(name or "").strip()
        if not term:
            return []
        if not 1<=int(limit)<=100:
            raise ValueError("道路搜索结果数须为1–100")
        where="data_usable AND trim(coalesce(street_name,''))<>'' AND lower(street_name) LIKE lower(?)"
        params=[f"%{term}%"]
        if district!="全部区域":
            where+=" AND district=?"
            params.append(district)
        frame=self.query(f"""SELECT district,arg_max(street_id,utci) AS street_id,street_name,
            list(DISTINCT street_id ORDER BY street_id) AS street_ids,
            count(DISTINCT street_id) AS segment_count,
            count(*) AS point_count,avg(utci) AS mean_utci,min(utci) AS min_utci,max(utci) AS max_utci,
            arg_max(point_id,utci) AS hottest_point_id,
            sum(CASE WHEN utci>32 THEN 1 ELSE 0 END) AS strong_heat_points
            FROM points WHERE {where} GROUP BY district,street_name
            ORDER BY mean_utci DESC,max_utci DESC,street_name,street_id LIMIT ?""",params+[int(limit)])
        results=records(frame)
        for row in results:
            row["street_ids"]=sorted(int(sid) for sid in (row.get("street_ids") or []))
            mean=float(row["mean_utci"]);maximum=float(row["max_utci"])
            count=int(row["point_count"]);strong=int(row["strong_heat_points"])
            row["mean_heat_grade"]=heat_stress_grade(mean)
            row["max_heat_grade"]=heat_stress_grade(maximum)
            row["strong_heat_share"]=strong/count if count else 0.0
            if mean>32:
                row["governance_status"]="建议纳入治理评估"
                row["governance_reason"]=f"道路分析位置的平均UTCI为{mean:.2f}°C，达到“{row['mean_heat_grade']}”；{strong}/{count}个位置达到强热应激及以上等级。"
            elif strong:
                row["governance_status"]="建议核查局部热段"
                row["governance_reason"]=f"道路平均UTCI为{mean:.2f}°C（{row['mean_heat_grade']}），但有{strong}/{count}个位置达到强热应激及以上等级，建议针对局部热段进一步评估。"
            else:
                row["governance_status"]="当前以常规观察为主"
                row["governance_reason"]=f"道路分析位置的平均UTCI为{mean:.2f}°C（{row['mean_heat_grade']}），当前没有位置达到强热应激及以上等级。"
        return results

    def nearby_named_roads(self,street_name,district,radius_m=1000,limit=8):
        """Measure source-road geometry distance in the source metre CRS."""
        import shapely
        from shapely.ops import transform
        from pyproj import Transformer
        project=Transformer.from_crs(4326,32650,always_xy=True).transform
        target=self.query('''SELECT geometry_json FROM roads WHERE street_id IN
            (SELECT DISTINCT street_id FROM points WHERE street_name=? AND district=? AND data_usable)''',[street_name,district])
        if target.empty:return []
        geometry=shapely.union_all([transform(project,shapely.geometry.shape(json.loads(g))) for g in target.geometry_json])
        lo_x,lo_y,hi_x,hi_y=geometry.bounds
        candidates=self.query('''SELECT DISTINCT r.street_id,r.street_name,r.geometry_json,p.district
            FROM roads r JOIN points p USING(street_id)
            WHERE p.data_usable AND p.x BETWEEN ? AND ? AND p.y BETWEEN ? AND ?
            AND trim(coalesce(r.street_name,''))<>'' AND r.street_name<>?''',
            [lo_x-radius_m,hi_x+radius_m,lo_y-radius_m,hi_y+radius_m,street_name])
        found={}
        for row in candidates.itertuples(index=False):
            road=transform(project,shapely.geometry.shape(json.loads(row.geometry_json)))
            distance=float(geometry.distance(road))
            if distance>radius_m:continue
            key=(row.street_name,row.district)
            if key not in found or distance<found[key]['distance_m']:
                found[key]={'street_name':row.street_name,'district':row.district,'distance_m':round(distance,1)}
        return sorted(found.values(),key=lambda r:(r['distance_m'],r['street_name']))[:limit]

    def search_road_policy(self, street_name, district="朝阳区"):
        """Retrieve relevant public policy/project pages for a named study road."""
        start=time.perf_counter()
        name=str(street_name or "").strip();area=str(district or "").strip()
        result=None
        if not name or name in ("未命名道路","未命名街段"):
            result={"status":"unnamed","street_name":name,"district":area,"results":[],"queries":[]}
        else:
            exact=self.query("""SELECT DISTINCT street_name FROM points
                WHERE district=? AND data_usable AND trim(coalesce(street_name,''))<>''
                AND lower(trim(street_name))=lower(trim(?)) ORDER BY street_name""",[area,name])
            if not exact.empty:
                resolved=str(exact.iloc[0]["street_name"])
            else:
                matches=self.query("""SELECT DISTINCT street_name FROM points
                    WHERE district=? AND data_usable AND trim(coalesce(street_name,''))<>''
                    AND lower(street_name) LIKE lower(?) ORDER BY street_name LIMIT 10""",[area,f"%{name}%"])
                names=[str(v) for v in matches.street_name.tolist()]
                if not names:
                    result={"status":"not_found","street_name":name,"district":area,"results":[],"queries":[]}
                elif len(names)>1:
                    result={"status":"ambiguous","street_name":name,"district":area,"matches":names,"results":[],"queries":[]}
                else:
                    resolved=names[0]
            if result is None:
                try:
                    point=records(self.query('SELECT * FROM points WHERE street_name=? AND district=? AND data_usable ORDER BY utci DESC,point_id LIMIT 1',[resolved,area]))[0]
                    references=self.road_references(point['point_id'])
                    ref_roads=[];seen=set()
                    for group in ('contrast','morphology'):
                        for row in references[group]['matches']:
                            if row.get('street_name') and row['street_name']!=resolved and row['street_name'] not in seen:
                                ref_roads.append(row);seen.add(row['street_name'])
                    nearby=self.nearby_named_roads(resolved,area)
                    result=search_named_road(resolved,area,self.web_search_api_key,
                        deepseek_api_key=getattr(self,'deepseek_api_key',''),nearby_roads=nearby,
                        reference_roads=ref_roads,problem_type=diagnose(point,self.no_new_trees)['problem_type'],no_new_trees=self.no_new_trees)
                    result['internal_references']=references
                except PolicySearchError as exc:
                    result={"status":"error","street_name":resolved,"district":area,"message":str(exc),"results":[],"queries":[]}
        return self._finish("search_road_policy",{"street_name":name,"district":area},result,start)

    def utci_range(self, district="全部区域"):
        """Return the observed UTCI range used to configure the UI."""
        if district == "全部区域":
            row = self.query("SELECT min(utci) AS lo,max(utci) AS hi FROM points WHERE data_usable").iloc[0]
        else:
            row = self.query("SELECT min(utci) AS lo,max(utci) AS hi FROM points WHERE data_usable AND district=?", [district]).iloc[0]
        return float(row["lo"]), float(row["hi"])

    def district_center(self, district="全部区域"):
        if district == "全部区域":
            row = self.query("SELECT avg(longitude) AS longitude,avg(latitude) AS latitude FROM points WHERE data_usable").iloc[0]
        else:
            row = self.query("SELECT avg(longitude) AS longitude,avg(latitude) AS latitude FROM points WHERE data_usable AND district=?", [district]).iloc[0]
        return float(row["longitude"]), float(row["latitude"])

    def _problem_diagnosis(self, row):
        return diagnose(row,self.no_new_trees)
    def find_hotspots(self,district="朝阳区",min_utci=None,count=5,existing_canopy_only=False,max_canopy_cover=1.0,min_canopy_cover=0.0):
        start=time.perf_counter()
        if min_utci is None:min_utci=self.turn_defaults.get("min_utci",self.utci_range()[0])
        valid=set(self.districts().district)|{"全部区域"}
        if district not in valid:raise ValueError("行政区不在研究范围内："+district)
        if not 1<=count<=20:raise ValueError("候选数量须为1–20")
        if not 0<=max_canopy_cover<=1 or not 0<=min_canopy_cover<=1 or not 0<=min_utci<=60:raise ValueError("阈值超出允许范围")
        existing_canopy_only=bool(existing_canopy_only)
        self.existing_only=existing_canopy_only
        min_canopy_cover=max(float(min_canopy_cover),self.min_existing_cover if existing_canopy_only else 0.0)
        args=dict(district=district,min_utci=float(min_utci),count=int(count),existing_canopy_only=existing_canopy_only,max_canopy_cover=float(max_canopy_cover),min_canopy_cover=min_canopy_cover)
        where="p.data_usable AND p.utci>=? AND p.canopy_cover<=? AND p.canopy_cover>=?"
        params=[min_utci,max_canopy_cover,min_canopy_cover]
        if district!="全部区域":where+=" AND p.district=?";params.append(district)
        if existing_canopy_only:where+=" AND p.canopy_cover>0 AND p.canopy_volume>0 AND p.canopy_height>0"
        stats=self.query(f"SELECT count(*) n,count(DISTINCT p.street_id) streets FROM points p WHERE {where}",params).iloc[0]
        type_frame=self.query(f"SELECT {type_sql('p')} AS type,count(*) n FROM points p WHERE {where} GROUP BY type",params)
        type_counts=dict(zip(type_frame.type,type_frame.n))
        cols="p.point_id,p.street_id,p.street_name,p.osm_id,p.district,p.longitude,p.latitude,p.x,p.y,p.utci,"+",".join("p."+k for k in FEATURES)+",s.street_point_count,s.street_mean_utci,s.street_max_utci"
        frame=self.query(f"""WITH candidates AS (
          SELECT {cols},row_number() OVER(PARTITION BY p.street_id, {type_sql('p')} ORDER BY p.utci DESC,p.point_id) rn
          FROM points p JOIN street_stats s USING(street_id,district) WHERE {where}
          ) SELECT * EXCLUDE(rn) FROM candidates WHERE rn=1 ORDER BY utci DESC,point_id""",params)
        ranked=[]
        for raw in records(frame):
            raw.update(self._problem_diagnosis(raw))
            ranked.append(raw)
        by_type={}
        for row in ranked:by_type.setdefault(row["problem_type"],[]).append(row)
        task=getattr(self,"turn_task",None)
        type_order=([TYPE_ORDER[1],TYPE_ORDER[2],TYPE_ORDER[3],TYPE_ORDER[0],TYPE_ORDER[4]]
                    if task and task["goal"]=="protect" else TYPE_ORDER)
        chosen=[]
        # One spatially separated representative per available decision bucket,
        # then fill remaining slots by the UTCI ranking.
        for key in type_order:
            for row in by_type.get(key,[]):
                if all(row["street_id"]!=r["street_id"] and math.hypot(row["x"]-r["x"],row["y"]-r["y"])>=150 for r in chosen):
                    chosen.append(row);break
            if len(chosen)==count:break
        if len(chosen)<count:
            for row in ranked:
                if row in chosen:continue
                if all(row["street_id"]!=r["street_id"] and math.hypot(row["x"]-r["x"],row["y"]-r["y"])>=150 for r in chosen):
                    chosen.append(row)
                    if len(chosen)==count:break
        selected_counts={}
        for row in chosen:selected_counts[row["problem_type"]]=selected_counts.get(row["problem_type"],0)+1
        type_summary=[]
        for key in type_order:
            rows=by_type.get(key,[])
            if rows:
                d=rows[0]
                type_summary.append({"problem_type":key,"name":d["problem_type_name"],"matching_points":int(type_counts.get(key,0)),"selected":selected_counts.get(key,0),"strategy_direction":d["strategy_direction"]})
        selection_note=("保护树冠任务先覆盖已有树冠条件的类型，再补充低覆盖等类型；不因禁止新增而过滤无树冠地点。"
                        if task and task["goal"]=="protect" else "")
        result={"scenario":SCENARIO,"filters":args,"matching_points":int(stats["n"]),"matching_streets":int(stats["streets"]),
                "selection_rule":selection_note+"先按透明规则覆盖不同高温问题类型，再在各类型内按UTCI降序取每街段代表点；代表点间距至少150m；街段均值另列。未纳入人口、预算或现场可行性。",
                "returned":len(chosen),"candidates":chosen,
                "problem_type_summary":type_summary,
                "classification_rule":CLASSIFICATION,
                "note":"无匹配时保留空结果。" if not chosen else "八项形态指标对应代表点10m缓冲区。"}
        return self._finish("find_hotspots",args,result,start)
    def select_location(self,point_id):
        """Select one real map location without pretending it is an area ranking."""
        start=time.perf_counter()
        frame=self.query("""SELECT p.*,s.street_point_count,s.street_mean_utci,s.street_max_utci
            FROM points p JOIN street_stats s USING(street_id,district)
            WHERE p.point_id=? AND p.data_usable""",[int(point_id)])
        if frame.empty:raise ValueError("该位置没有可用研究数据")
        row=records(frame)[0];row.update(self._problem_diagnosis(row))
        filters=dict(district=row['district'],min_utci=self.utci_range()[0],count=1,
                     existing_canopy_only=False,max_canopy_cover=1.,min_canopy_cover=0.)
        self.preserve_targets=False
        result=dict(scenario=SCENARIO,filters=filters,matching_points=1,matching_streets=1,returned=1,
            candidates=[row],selection_source='user_map_selection',
            selection_rule='由用户在真实路网中指定该分析位置；本次未进行全区优先级筛选。',
            classification_rule=CLASSIFICATION,
            problem_type_summary=[dict(problem_type=row['problem_type'],name=row['problem_type_name'],
                matching_points=1,selected=1,strategy_direction=row['strategy_direction'])],
            note='八项指标对应指定位置的10 m缓冲区。')
        return self._finish('select_location',{'point_id':int(point_id)},result,start)

    def analyze_morphology(self,point_ids):
        start=time.perf_counter()
        ids=list(dict.fromkeys(int(v) for v in point_ids))
        if not ids or len(ids)>20:raise ValueError("需要1–20个真实point_id")
        frame=self.query("SELECT * FROM points WHERE point_id IN ("+",".join("?" for _ in ids)+")",ids)
        if set(frame.point_id)!=set(ids):raise ValueError("包含不存在于建模表的编号")
        result=[]
        for r in records(frame):
            c=r["canopy_cover"];v=r["canopy_volume"]
            diagnosis=strategy_options(r,self.no_new_trees)
            action=diagnosis["strategy_direction"]
            result.append({"point_id":r["point_id"],"street_id":r["street_id"],"utci":r["utci"],
               "metrics":{k:{"value":r[k],"label":label,"unit":unit} for k,(label,unit) in FEATURES.items()},
               "morphology_note":f"问题类型：{diagnosis['problem_type_name']}；树冠覆盖率{c:.1%}；冠层体积{v:.1f} m³；建筑覆盖率{r['building_cover']:.1%}。",
               "candidate_action":action,"evidence_ids":["E04","E05"],
               "problem_type":diagnosis["problem_type"],"problem_type_name":diagnosis["problem_type_name"],
               "heat_stress_grade":diagnosis["heat_stress_grade"],
               "problem_diagnosis":diagnosis["problem_diagnosis"],"strategy_direction":diagnosis["strategy_direction"],
               "decision_evidence_ids":diagnosis["decision_evidence_ids"],"diagnosis_rule":diagnosis["diagnosis_rule"],
               "options":diagnosis["options"],"indicator_evidence":diagnosis["indicator_evidence"],
               "priority_label":diagnosis["priority_label"],"priority_basis":diagnosis["priority_basis"],
               "no_new_trees":diagnosis["no_new_trees"],
               "existing_canopy_present":diagnosis["existing_canopy_present"],
               "field_checks":["实际树冠与数据年份是否一致","树木健康与根系/土壤条件","土地权属、管线和养护权限","现场风、湿度、辐射和人流暴露"],
               "limits":""})
        supported=[r["point_id"] for r in result if r["existing_canopy_present"]]
        priority=[r["point_id"] for r in result if next(o for o in r["options"] if o["code"]=="grow_existing")["status"]=="优先评估"]
        return self._finish("analyze_morphology",{"point_ids":ids},{"buffer_m":10,"existing_only":self.existing_only,"points":result,
            "canopy_support_summary":{"has_existing_canopy_indicators":supported,"prioritize_canopy_investigation":priority}},start)
    def find_similar_streets(self,point_id,count=3,direction="different",scope="district",_record=True):
        start=time.perf_counter()
        if not 1<=count<=5:raise ValueError("参考案例数量须为1–5")
        if direction not in ("different","higher_cover","higher_volume"):raise ValueError("未知树冠对照方向")
        if scope not in ('district','city'):raise ValueError('未知检索范围')
        target=self.query("SELECT * FROM points WHERE point_id=?",[int(point_id)])
        if target.empty:raise ValueError("测点不存在")
        t=records(target)[0]
        transforms={"building_cover":"building_cover","building_height":"building_height","building_variance":"ln(1+building_variance)","building_volume":"ln(1+building_volume)"}
        scope_where='district=? AND ' if scope=='district' else ''
        scope_params=[t['district']] if scope=='district' else []
        scale=self.query("SELECT "+",".join(f"stddev_pop({v}) AS {k}" for k,v in transforms.items())+" FROM points WHERE "+scope_where+"data_usable",scope_params).iloc[0]
        terms=[];params=[]
        for k,expr in transforms.items():
            value=math.log1p(t[k]) if k in ("building_variance","building_volume") else t[k]
            std=float(scale[k]);std=std if math.isfinite(std) and std>1e-8 else 1.0
            terms.append(f"pow(({expr}-?)/?,2)");params += [value,std]
        distance="sqrt(("+ "+".join(terms)+")/4)"
        cols="point_id,street_id,street_name,district,longitude,latitude,utci,"+",".join(FEATURES)
        if direction=="higher_cover":
            canopy_condition="canopy_cover>=?+0.10";canopy_params=[t["canopy_cover"]]
            direction_text="树冠覆盖率至少高10个百分点"
        elif direction=="higher_volume":
            canopy_condition="canopy_volume>=?+100 AND canopy_cover>=?-0.05";canopy_params=[t["canopy_volume"],t["canopy_cover"]]
            direction_text="参考冠体积≥目标冠体积+100m³，且参考覆盖率≥目标覆盖率−0.05（最多低5个百分点）"
        else:
            canopy_condition="abs(canopy_cover-?)>=0.10";canopy_params=[t["canopy_cover"]]
            direction_text="树冠覆盖率绝对差至少10个百分点（双向）"
        sql=f"""WITH scored AS (
          SELECT {cols},{distance} AS morphology_distance,
          sqrt(pow(x-?,2)+pow(y-?,2)) AS separation_m
          FROM points WHERE {scope_where}data_usable AND street_id<>?
          AND {canopy_condition} AND abs(building_cover-?)<=0.05
          AND abs(building_height-?)<=5
        ), distinct_streets AS (
          SELECT *,row_number() OVER(PARTITION BY street_id ORDER BY morphology_distance,separation_m,point_id) rn
          FROM scored WHERE morphology_distance<=0.75 AND separation_m>=150
        ) SELECT * EXCLUDE(rn),count(*) OVER() AS eligible_streets,
          (SELECT count(*) FROM scored WHERE morphology_distance<=0.75 AND separation_m>=150) AS eligible_points
          FROM distinct_streets WHERE rn=1
          ORDER BY morphology_distance,separation_m,point_id LIMIT ?"""
        params += [t["x"],t["y"]]+scope_params+[t["street_id"]]+canopy_params+[t["building_cover"],t["building_height"],count]
        matches=records(self.query(sql,params))
        for r in matches:
            r["utci_difference_reference_minus_target"]=round(r["utci"]-t["utci"],4)
            r["canopy_cover_difference"]=round(r["canopy_cover"]-t["canopy_cover"],6)
            r["metric_differences"]={k:round(r[k]-t[k],6) for k in FEATURES}
        result={"target_point_id":int(point_id),"target_utci":t["utci"],"matches":matches,
            "direction":direction,"direction_definition":direction_text+"。",
            "eligible_points":int(matches[0]["eligible_points"]) if matches else 0,
            "eligible_streets":int(matches[0]["eligible_streets"]) if matches else 0,
            "returned":len(matches),"target_metrics":{k:t[k] for k in FEATURES},
            "matching_conditions":{"building_cover_max_difference":0.05,"building_height_max_difference_m":5,"building_rms_max":0.75,"separation_min_m":150,
                "reference_canopy_cover_min":t["canopy_cover"]+.1 if direction=="higher_cover" else (max(0.,t["canopy_cover"]-.05) if direction=="higher_volume" else None),
                "reference_canopy_volume_min":t["canopy_volume"]+100 if direction=="higher_volume" else None},
            "matching_method":("同区" if scope=='district' else "北京五环内全域")+"、不同源街段、相距≥150m；建筑覆盖差≤0.05、平均高度差≤5m；四项建筑特征标准化RMS距离≤0.75，体积/方差先log1p；"+direction_text+"。按形态距离、地理距离、点ID排序，不按UTCI结果筛选。",
            "difference_definition":"参考点UTCI－目标点UTCI；正值表示参考点更高，负值表示参考点更低。",
            "evidence_ids":["E06"],"note":"未检索到符合条件的参考道路。" if not matches else "参考道路已按建筑形态、树冠配置与空间间距完成匹配。"}
        result['scope']=scope
        return self._finish("find_similar_streets",{"point_id":int(point_id),"count":count,"direction":direction,"scope":scope},result,start) if _record else result

    def find_morphology_peers(self,point_id,count=3,_record=True):
        """Search all eight standardized features, without ranking on UTCI."""
        start=time.perf_counter()
        if not 1<=count<=5:raise ValueError('参考案例数量须为1–5')
        target=self.query('SELECT * FROM points WHERE point_id=? AND data_usable',[int(point_id)])
        if target.empty:raise ValueError('分析位置不存在')
        t=records(target)[0]
        transforms={k:f'ln(1+{k})' if k.endswith(('volume','variance')) else k for k in FEATURES}
        scale=self.query('SELECT '+','.join(f'stddev_pop({v}) AS {k}' for k,v in transforms.items())+' FROM points WHERE data_usable').iloc[0]
        terms=[];params=[]
        for k,expr in transforms.items():
            value=math.log1p(t[k]) if k.endswith(('volume','variance')) else t[k]
            std=float(scale[k]);std=std if math.isfinite(std) and std>1e-8 else 1.
            terms.append(f'pow(({expr}-?)/?,2)');params.extend([value,std])
        distance='sqrt(('+ '+'.join(terms)+')/8)'
        cols='point_id,street_id,street_name,district,longitude,latitude,utci,'+','.join(FEATURES)
        params.extend([t['x'],t['y'],t['street_id'],count])
        matches=records(self.query(f'''WITH scored AS (
            SELECT {cols},{distance} AS morphology_distance,sqrt(pow(x-?,2)+pow(y-?,2)) AS separation_m
            FROM points WHERE data_usable AND street_id<>?
        ), eligible AS (SELECT * FROM scored WHERE morphology_distance<=0.75 AND separation_m>=150),
        grouped AS (SELECT *,row_number() OVER(PARTITION BY coalesce(nullif(trim(street_name),''),cast(street_id AS VARCHAR)),district
            ORDER BY morphology_distance,separation_m,point_id) rn FROM eligible)
        SELECT * EXCLUDE(rn),count(*) OVER() AS eligible_streets,(SELECT count(*) FROM eligible) AS eligible_points
        FROM grouped WHERE rn=1 ORDER BY morphology_distance,separation_m,point_id LIMIT ?''',params))
        for r in matches:
            r['utci_difference_reference_minus_target']=round(r['utci']-t['utci'],4)
            r['metric_differences']={k:round(r[k]-t[k],6) for k in FEATURES}
        result={'target_point_id':int(point_id),'target_utci':t['utci'],'target_metrics':{k:t[k] for k in FEATURES},
            'matches':matches,'returned':len(matches),'eligible_points':int(matches[0]['eligible_points']) if matches else 0,
            'eligible_streets':int(matches[0]['eligible_streets']) if matches else 0,'scope':'city',
            'matching_method':'北京五环内全域；八项指标标准化，体积和方差先log1p；RMS距离≤0.75、不同源街段且相距≥150m。按形态距离排序，每条命名道路展示一个参考位置。',
            'matching_conditions':{'feature_count':8,'rms_max':0.75,'separation_min_m':150}}
        return self._finish('find_morphology_peers',{'point_id':int(point_id),'count':count},result,start) if _record else result

    def road_references(self,point_id):
        return {'morphology':self.find_morphology_peers(point_id,_record=False),
            'contrast':self.find_similar_streets(point_id,direction='different',scope='city',_record=False)}

    def compare_locations(self,point_ids):
        start=time.perf_counter()
        ids=list(dict.fromkeys(int(i) for i in point_ids))
        if not 2<=len(ids)<=5:raise ValueError("比较需要2至5个真实测点编号")
        rows=records(self.query("SELECT point_id,street_id,district,utci,"+",".join(FEATURES)+" FROM points WHERE point_id IN ("+",".join("?" for _ in ids)+")",ids))
        if len(rows)!=len(ids):raise ValueError("比较包含不存在的测点")
        rows.sort(key=lambda r:ids.index(r["point_id"]))
        old=self.last_report or {}; selected={r["point_id"] for r in old.get("hotspots",{}).get("candidates",[])}
        for r in rows:
            r.update(strategy_options(r,self.no_new_trees))
            r["in_current_plan"]=r["point_id"] in selected
        return self._finish("compare_locations",{"point_ids":ids},{"points":rows,
            "selection_rule":old.get("hotspots",{}).get("selection_rule","尚无候选方案"),
            "limit":"按热应激、八项形态指标和措施适用性逐点对照。"},start)
    def get_research_evidence(self,query,limit=4):
        start=time.perf_counter()
        if not 1<=limit<=8:raise ValueError("证据数须为1–8")
        cards=evidence_cards()
        asks_limits="E07" in query.upper() or any(k in query for k in ("局限","研究边界","方法边界"))
        if not asks_limits:cards=[e for e in cards if e["id"]!="E07"]
        ranked=sorted(cards,key=lambda e:-(20*int(e["id"].lower() in query.lower())+sum(tag.lower() in query.lower() for tag in e["tags"])))
        selected=ranked[:limit]
        if asks_limits:
            if not any(e["id"]=="E07" for e in selected):selected.append(next(e for e in cards if e["id"]=="E07"))
        result={"query":query,"evidence":[{k:e[k] for k in ("id","title","section","summary","source_paragraphs","evidence_type")} for e in selected],
                "provenance":"依据用户提供并已发表的论文整理，原文段落与章节编号可追溯。"}
        return self._finish("get_research_evidence",{"query":query,"limit":limit},result,start)
    def road_heat_segments(self, district="全部区域"):
        from heatplan.network import build_network
        segments,self.network_audit=build_network(self,district)
        return segments
    def road_geojson(self,street_ids):
        if not street_ids:return {"type":"FeatureCollection","features":[]}
        df=self.query("SELECT * FROM roads WHERE street_id IN ("+",".join("?" for _ in street_ids)+")",street_ids)
        return {"type":"FeatureCollection","features":[{"type":"Feature","geometry":json.loads(r["geometry_json"]),"properties":{"street_id":r["street_id"],"name":r["street_name"]}} for r in records(df)]}
