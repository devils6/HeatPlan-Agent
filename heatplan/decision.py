"""Reproducible evidence-to-option rules, separate from language-model reasoning."""
from heatplan.config import FEATURES

TYPE_ORDER = ["low_canopy_high_heat", "existing_canopy_low_volume", "high_canopy_persistent_heat", "canopy_building_context", "below_strong_heat"]
TYPE_NAMES = dict(zip(TYPE_ORDER, ["低覆盖热应激", "已有覆盖但冠体积偏小", "较高树冠条件下仍有热应激", "建筑与树冠组合待核查", "未达强热应激分级"] ))
TYPE_COLORS = dict(zip(TYPE_ORDER, [[225,119,44,230],[139,95,190,230],[208,65,85,230],[35,126,157,230],[159,171,168,160]]))
CLASSIFICATION = "先按论文分级识别热应激，再依据树冠覆盖率和冠层体积区间划分不同问题类型；分类规则公开、可复现。"

def heat_stress_grade(utci):
    value=float(utci)
    return "未达强热应激" if value<=32 else ("强热应激" if value<=38 else ("很强热应激" if value<=46 else "极强热应激"))

def type_sql(prefix=""):
    p = prefix + "." if prefix else ""
    return (f"CASE WHEN {p}utci<=32 THEN 'below_strong_heat' WHEN {p}canopy_cover<0.20 THEN 'low_canopy_high_heat' "
            f"WHEN {p}canopy_volume<500 THEN 'existing_canopy_low_volume' "
            f"WHEN {p}canopy_cover>=0.80 OR {p}canopy_volume>=2000 THEN 'high_canopy_persistent_heat' ELSE 'canopy_building_context' END")

def type_key(row):
    if float(row.get("utci", 38.0)) <= 32: return TYPE_ORDER[4]
    if float(row["canopy_cover"]) < .2: return TYPE_ORDER[0]
    if float(row["canopy_volume"]) < 500: return TYPE_ORDER[1]
    if float(row["canopy_cover"]) >= .8 or float(row["canopy_volume"]) >= 2000: return TYPE_ORDER[2]
    return TYPE_ORDER[3]

def diagnose(row, no_new_trees=False):
    c,v=float(row["canopy_cover"]),float(row["canopy_volume"])
    key=type_key(row)
    observations = {
        TYPE_ORDER[0]: f"树冠覆盖{c:.1%}，体积{v:.1f}m³；优先比较遮阴连续性与补植组合。",
        TYPE_ORDER[1]: f"覆盖{c:.1%}已有一定基础，冠体积{v:.1f}m³；优先比较现有树冠培育方向。",
        TYPE_ORDER[2]: f"覆盖{c:.1%}、体积{v:.1f}m³；优先保护树冠并比较遮阴协同方向。",
        TYPE_ORDER[3]: f"覆盖{c:.1%}、体积{v:.1f}m³；结合建筑条件比较绿化治理方向。",
        TYPE_ORDER[4]: f"模拟UTCI {float(row.get('utci',32)):.2f}°C；作为背景对照。",
    }
    directions={
        TYPE_ORDER[0]: "比较遮阴连续性与补植组合，优先查看建筑投影与道路空间。",
        TYPE_ORDER[1]: "保护并培育现有树冠，优先查看土壤、根系和修剪条件。",
        TYPE_ORDER[2]: "保护现有树冠，比较遮阴时段与建筑协同方向。",
        TYPE_ORDER[3]: "结合建筑与树冠指标，比较相似道路的绿化方向。",
        TYPE_ORDER[4]: "保留为背景对照与监测对象。",
    }
    if no_new_trees and key==TYPE_ORDER[0]:
        directions[key] = ("仅评估现有树冠培育；本轮不安排新增树木。" if c>0 and v>0 else
                           "本轮不安排新增树木，保留该位置开展建筑与环境协同治理比较。")
    return {"problem_type":key,"problem_type_name":TYPE_NAMES[key],"problem_diagnosis":observations[key],"heat_stress_grade":heat_stress_grade(row["utci"]),
            "strategy_direction":directions[key],"decision_evidence_ids":["E04","E05","E06","E08"],"diagnosis_rule":CLASSIFICATION}

def strategy_options(row, no_new_trees=False):
    """A comparison, with evidence and disqualifying constraints for each option."""
    d=diagnose(row,no_new_trees); key=d["problem_type"]
    c,v=float(row["canopy_cover"]),float(row["canopy_volume"])
    bc,bh,bv=(float(row[k]) for k in ("building_cover","building_height","building_volume"))
    existing=c>0 and v>0 and float(row["canopy_height"])>0
    background=key==TYPE_ORDER[4]
    options=[]
    def add(code,name,status,reason,ids,checks):
        options.append(dict(code=code,name=name,status=status,reason=reason,evidence_ids=ids,field_requirements=checks))
    add("add_cover","补植与连续遮阴", "约束排除" if no_new_trees else ("暂缓" if background or c>=.8 else ("优先评估" if c<.4 else "备选")),
        f"覆盖{c:.1%}；论文中的20%及40%–80%覆盖区间用于比较候选措施。"+("管理要求禁止新增树木。" if no_new_trees else "优先比较补植与连续遮阴方向。"),
        ["E04"],["土地与管线许可","建筑投影及人行道遮阴连续性"])
    add("grow_existing","保护与培育现有树冠", "暂缓" if background else ("优先评估" if existing and v<500 else ("备选" if existing else "证据不足")),
        (f"覆盖{c:.1%}、体积{v:.1f}m³、平均高度{float(row['canopy_height']):.2f}m，已有树冠指标支持评估保护与培育方向。"
         if existing else
         f"当前覆盖率为{c:.1%}、冠层体积为{v:.1f}m³、平均高度为{float(row['canopy_height']):.2f}m；这三项指标未同时显示可供养护的现有树冠基础，因此暂不推荐保护与培育现有树冠。"),
        ["E04","E05"],["树种与健康现场调查","土壤根系空间与修剪记录"])
    add("audit_context","建筑与树冠协同核查", "优先评估" if not background and (key in TYPE_ORDER[2:4] or (no_new_trees and not existing)) else "备选",
        f"建筑覆盖{bc:.1%}、高度{bh:.2f}m、体积{bv:.1f}m³；建筑总体关联呈非线性，结合四项建筑指标比较遮阴协同。",
        ["E06"],["遮阴路径与太阳时段","辐射与风湿测量；仅核查，不建议改变建筑"])
    add("defer","暂缓具体施工并补调查", "优先评估" if background or (no_new_trees and not existing) else "备选",
        "先整理工程条件与干预方案，再进入施工评估。",["E08"],["权属、预算、暴露人群","树冠、道路与建筑协同条件"])
    coverage_band="<10%" if bc<.1 else ("10%–20%" if bc<.2 else ("20%–40%" if bc<=.4 else ">40%"))
    signals=[]
    for feature,(label,unit) in FEATURES.items():
        value=float(row[feature])
        if "variance" in feature:
            interpretation="论文显示树冠高度方差与UTCI的总体关联较弱。"
        elif feature=="building_cover": interpretation=f"位于{coverage_band}区间；论文显示建筑覆盖率与UTCI关联呈非线性，可结合遮阴协同比较治理方向。"
        elif feature.startswith("building_"): interpretation="用于比较建筑条件相近道路的形态差异与遮阴协同。"
        elif feature=="canopy_height": interpretation="论文结果显示树冠平均高度与UTCI总体关联随高度增加而下降。" if value<=20 else "该指标值高于论文主要分析区间。"
        else: interpretation="参见论文覆盖/体积区间与备选策略；该指标用于当前位置的证据对照。"
        evidence="E06" if feature.startswith("building_") else ("E04" if feature=="canopy_cover" else "E05")
        signals.append(dict(feature=feature,label=label,value=value,unit=unit,interpretation=interpretation,evidence_id=evidence))
    return {**d,"options":options,"indicator_evidence":signals,
            "existing_canopy_present":existing,
            "no_new_trees":no_new_trees,"priority_label":"优先调查，暂缓施工" if background or (no_new_trees and not existing) or key==TYPE_ORDER[2] else "优先评估绿化可行性",
            "priority_basis":"综合热应激分级、问题类型、形态指标与治理约束生成候选优先级。"}
