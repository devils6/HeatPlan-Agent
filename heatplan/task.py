"""Translate management intent into an explicit, reproducible task contract."""
import math
from copy import deepcopy

GOALS = {"综合治理诊断": "comprehensive", "优先改善低树冠覆盖": "low_cover", "保护与改善现有树冠": "protect", "相似街道对照": "compare"}
RISKS = ["全部热应激等级", "强热应激及以上（>32°C）", "非常强热应激（>38°C）", "自定义阈值"]
CONSTRAINTS = ["仅绿化改善，不调整建筑", "不新增树木，保护与改善现有树冠", "由 AI 根据证据选择候选方向"]
DIRECTIONS = {"更高的树冠覆盖": "higher_cover", "更大的冠层体积": "higher_volume", "不同的树冠配置": "different"}

def make_task(district, goal, risk, constraint, count, observed_min, custom_min=None, max_cover=100, direction="higher_cover"):
    lower = float(observed_min)
    if risk == RISKS[1]: lower = math.nextafter(32., math.inf)
    elif risk == RISKS[2]: lower = math.nextafter(38., math.inf)
    elif risk == RISKS[3]: lower = float(custom_min if custom_min is not None else observed_min)
    if lower < observed_min or lower > 60: raise ValueError("自定义下限应在数据最小值至60°C之间")
    if not 1 <= int(count) <= 20 or not 0 <= max_cover <= 100: raise ValueError("任务参数超出范围")
    upper = float(max_cover) / 100
    if GOALS[goal] == "low_cover": upper = min(upper, math.nextafter(.2, -math.inf))
    no_new = constraint == CONSTRAINTS[1] or GOALS[goal] == "protect"
    effective_constraint = CONSTRAINTS[1] if no_new else constraint
    filters = dict(district=district, min_utci=lower, count=int(count), existing_canopy_only=False,
                   max_canopy_cover=upper, min_canopy_cover=0.)
    return dict(goal=GOALS[goal], goal_label=goal, risk=risk, constraint=effective_constraint,
                no_new_trees=no_new, filters=filters,
                reference_direction=direction if GOALS[goal] == "compare" else None,
                advanced_max_cover=float(max_cover))

def selection_key(task):
    return (task["goal"], tuple(sorted(task["filters"].items())), task.get("reference_direction"))

def task_matches_report(task, report):
    if not report or not report.get("task"): return False
    done = report["task"]
    return selection_key(task) == selection_key(done) and task["constraint"] == done["constraint"] and task["no_new_trees"] == report["policy"]["no_new_trees"]

def requires_selection(task, previous):
    return not previous or not previous.get("task") or selection_key(task) != selection_key(previous["task"])

def task_prompt(task, previous=None):
    f = task["filters"]
    risk = task["risk"] if task["risk"] != RISKS[3] else f"UTCI ≥ {f['min_utci']:g}°C"
    parts = [f"请分析{f['district']}的城市高温问题。治理目标：{task['goal_label']}；热应激范围：{risk}；措施约束：{task['constraint']}；希望获得{f['count']}个候选分析位置。"]
    if task["goal"] == "low_cover": parts.append("低覆盖任务直接筛选树冠覆盖率<20%的道路；不符合条件的道路不进入候选，再对符合条件的位置比较八项形态指标和措施。")
    if task["goal"] == "protect": parts.append("优先检查已有树冠条件的空间类型；不新增树木只约束措施，不删除无树冠的地点。")
    if task.get("reference_direction"):
        label = next(k for k,v in DIRECTIONS.items() if v == task["reference_direction"])
        parts.append(f"为各候选寻找建筑条件相似、{label}的参考案例，工具方向使用{task['reference_direction']}。")
    if task["advanced_max_cover"] < 100: parts.append(f"高级筛选：树冠覆盖率不超过{task['advanced_max_cover']:g}%。")
    parts.append("综合八项城市形态指标、原论文SHAP关联与真实参考案例，比较备选措施、证据及现场核查需求；没有符合条件的数据时保留空结果。")
    if requires_selection(task, previous): parts.append("本轮重新选点并执行相关查询；不得沿用不符合当前任务的旧候选。")
    else: parts.append("本轮保留原地点，重新比较当前约束下的措施；参考案例与论文证据可注明来源后复用。")
    return "\n".join(parts)

def adopt_report_task(report, fallback):
    """Reflect actual tool results after a natural-language change of scope."""
    task = deepcopy(report.get("task") or fallback)
    actual = report["hotspots"]["filters"]
    if actual != task["filters"]:
        task["goal"] = "comprehensive"; task["goal_label"] = "综合治理诊断"
        task["risk"] = RISKS[3]; task["reference_direction"] = None
        task["advanced_max_cover"] = actual["max_canopy_cover"] * 100
    task["filters"] = deepcopy(actual)
    task["no_new_trees"] = report["policy"]["no_new_trees"]
    if task["no_new_trees"]: task["constraint"] = CONSTRAINTS[1]
    elif task["constraint"] == CONSTRAINTS[1]: task["constraint"] = CONSTRAINTS[0]
    if task["goal"] == "protect" and not task["no_new_trees"]:
        task["goal"] = "comprehensive"; task["goal_label"] = "综合治理诊断"
    return task
