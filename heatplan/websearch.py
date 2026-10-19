"""Official-site web search for named-road policies and engineering projects."""
import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class PolicySearchError(RuntimeError):
    pass


TOPICS = (
    ("城市更新与老旧街区改造", "周边片区 2021年以来 城市更新 老旧城区改造 老旧小区改造 实施方案 改造项目"),
    ("生态廊道与道路绿化", "周边 2021年以来 生态廊道 绿道建设 道路绿化 行道树 绿化提升 工程项目"),
)

PROJECT_TERMS = ("工程", "项目", "开工", "竣工", "建成", "施工", "改造", "提升")
POLICY_TERMS = ("意见", "方案", "通知", "规划", "办法", "政策", "导则", "条例")


def _published_date(value):
    """Return a normalized date, or None when the search provider cannot date a page."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        try:
            return parsedate_to_datetime(raw).date()
        except (TypeError, ValueError, OverflowError):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
            except ValueError:
                return None


def _project_year(title, snippet):
    """Find a recent year explicitly mentioned in a project page's visible text."""
    text = f"{title} {snippet}"
    years = [int(match) for match in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)]
    if not years or any(year < 2021 for year in years):
        return None
    recent = [year for year in years if year >= 2021]
    return max(recent) if recent else None


def _is_project_result(title, snippet):
    text = f"{title} {snippet}"
    return any(term in text for term in PROJECT_TERMS) and not any(term in title for term in POLICY_TERMS)


def _search(query, api_key, max_results):
    body = json.dumps({
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "topic": "general",
        "include_answer": False,
        "include_raw_content": False,
        "include_published_date": True,
        "start_date": "2021-01-01",
        "filter_by_published_date": True,
        "country": "china",
        "language": "zh-cn",
    }, ensure_ascii=False).encode("utf-8")
    request = Request(
        "https://api.tavily.com/search", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            raise PolicySearchError("网页检索密钥无效，请在“设置与说明”中更新。") from exc
        if exc.code == 429:
            raise PolicySearchError("网页检索服务暂时达到频率或额度上限，请稍后重试。") from exc
        raise PolicySearchError(f"网页检索服务返回错误（HTTP {exc.code}）。") from exc
    except URLError as exc:
        raise PolicySearchError("暂时无法连接网页检索服务，请检查网络后重试。") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise PolicySearchError("网页检索服务响应超时或格式异常，请稍后重试。") from exc


def _is_government_url(url):
    try:
        parsed = urlparse(str(url))
        host = (parsed.hostname or "").lower().rstrip(".")
        return parsed.scheme == "https" and (host == "gov.cn" or host.endswith(".gov.cn"))
    except ValueError:
        return False


def search_named_road(street_name, district, api_key, max_results_per_topic=5):
    """Search recent government sources and require an explicit post-2020 year for projects."""
    name = str(street_name or "").strip()
    area = str(district or "").strip()
    if not name or name in ("未命名道路", "未命名街段"):
        return {"status": "unnamed", "street_name": name, "district": area, "results": [], "queries": []}
    if not api_key:
        return {"status": "not_configured", "street_name": name, "district": area, "results": [], "queries": []}

    results, queries, seen = [], [], set()
    for category, terms in TOPICS:
        query = f'北京{area} “{name}” {terms}'
        queries.append(query)
        response = _search(query, api_key, max_results_per_topic)
        for item in response.get("results", []):
            url = str(item.get("url") or "").strip()
            if not _is_government_url(url) or url in seen:
                continue
            published = _published_date(item.get("published_date"))
            if published is None or published < date(2021, 1, 1):
                continue
            title = str(item.get("title") or "未命名资料")
            snippet = str(item.get("content") or "").strip()
            is_project = _is_project_result(title, snippet)
            project_year = _project_year(title, snippet) if is_project else None
            if is_project and project_year is None:
                continue
            seen.add(url)
            results.append({
                "category": category,
                "title": title,
                "url": url,
                "domain": urlparse(url).hostname or "",
                "snippet": snippet,
                "published_date": published.isoformat(),
                "source_type": "改造项目" if is_project else "政策资料",
                "project_year": project_year,
                "score": float(item.get("score") or 0),
            })
    return {
        "status": "ok" if results else "no_results",
        "street_name": name,
        "district": area,
        "queries": queries,
        "results": results,
    }
