"""
匹配评分：硬筛 + LLM 四维评分（skill_match, experience_match, growth_potential, culture_fit）
"""
import re
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
logger = logging.getLogger(__name__)

# 公司属性 code -> 中文（用于黑名单映射）
COMPANY_TYPE_CODE_TO_CN = {
    "ai_native": "AI原生公司",
    "internet_ai": "互联网大厂AI业务线",
    "traditional_digital": "传统企业数字化转型",
    "ai_vertical": "AI+垂直行业",
    "outsourcing": "外包/咨询",
    "other": "其他",
}

# 每批发送给 LLM 的岗位条数（小批量试）
MATCH_BATCH_SIZE = int(os.getenv("MATCH_BATCH_SIZE", "3"))
if MATCH_BATCH_SIZE < 1:
    MATCH_BATCH_SIZE = 1
elif MATCH_BATCH_SIZE > 5:
    MATCH_BATCH_SIZE = 5


def _get_model_config(model_id: str) -> Tuple[str, str, str]:
    configs = {
        "supermind": (
            os.getenv("SUPER_MIND_API_KEY"),
            os.getenv("SUPER_MIND_BASE_URL"),
            os.getenv("SUPER_MIND_MODEL"),
        ),
        "deepseek_chat": (
            os.getenv("DEEPSEEK_API_KEY"),
            os.getenv("DEEPSEEK_BASE_URL"),
            os.getenv("DEEPSEEK_MODEL_CHAT", "deepseek-chat"),
        ),
        "deepseek_reasoner": (
            os.getenv("DEEPSEEK_API_KEY"),
            os.getenv("DEEPSEEK_BASE_URL"),
            os.getenv("DEEPSEEK_MODEL_REASONER", "deepseek-reasoner"),
        ),
    }
    cfg = configs.get(model_id)
    if not cfg or not all(cfg):
        raise ValueError(f"模型 {model_id} 未在 .env 中完整配置")
    return cfg


def _parse_salary_range(salary_desc: str) -> Optional[Tuple[float, float]]:
    """解析薪资描述，返回 (min_k, max_k) 或 None"""
    if not salary_desc or not isinstance(salary_desc, str):
        return None
    s = salary_desc.strip()
    if "面议" in s or "不限" in s:
        return None
    # 匹配 20-50K、20k-50k、3k以下、50k以上
    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK]?\s*[-~至到]\s*(\d+(?:\.\d+)?)\s*[kK]?", s)
    if m:
        low = float(m.group(1))
        high = float(m.group(2))
        if low > high:
            low, high = high, low
        return (low, high)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK]?\s*以下", s)
    if m:
        return (0, float(m.group(1)))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK]?\s*以上", s)
    if m:
        return (float(m.group(1)), 999)
    return None


def _salary_overlap(
    job_min: float, job_max: float,
    pref_min: float, pref_max: float,
) -> bool:
    """判断岗位薪资区间与偏好区间是否有重合"""
    if pref_min <= 0 and pref_max <= 0:
        return True
    return not (job_max < pref_min or job_min > pref_max)


def _city_match(job_city: str, target_cities: List[str]) -> bool:
    """岗位城市是否在目标城市列表中"""
    if not target_cities:
        return True
    job = (job_city or "").strip()
    for tc in target_cities:
        tc = (tc or "").strip()
        if not tc:
            continue
        if tc == job or tc in job or job in tc:
            return True
        if tc == "远程" and ("远程" in job or "全国" in job or not job):
            return True
    return False


def _company_not_blacklisted(job_company_type: str, blacklist_codes: List[str]) -> bool:
    """岗位公司类型不在黑名单中"""
    if not blacklist_codes:
        return True
    job_cn = (job_company_type or "").strip()
    for code in blacklist_codes:
        cn = COMPANY_TYPE_CODE_TO_CN.get(code, code)
        if cn == job_cn:
            return False
    return True


def hard_filter_jobs(
    jobs: List[Dict],
    target_salary_min: float,
    target_salary_max: float,
    target_cities: List[str],
    company_type_blacklist: List[str],
) -> Tuple[List[Dict], Dict[str, Any]]:
    """
    硬筛：薪资区间重合、城市匹配、公司类型不在黑名单。

    返回 (通过列表, 统计信息)。
    统计字段：input 入参条数；drop_salary 薪资区间不重合（仅当 JD 薪资可解析且与偏好无交集）；
    drop_city 城市不符；drop_blacklist 命中公司类型黑名单；pass 通过硬筛条数。
    """
    stats: Dict[str, Any] = {
        "input": len(jobs),
        "drop_salary": 0,
        "drop_city": 0,
        "drop_blacklist": 0,
        "pass": 0,
    }
    result: List[Dict] = []
    for job in jobs:
        # 薪资（可解析且与偏好区间无重合则剔除；无法解析薪资则本项不拦）
        sr = _parse_salary_range(job.get("salary_desc") or "")
        if sr:
            j_min, j_max = sr
            if not _salary_overlap(j_min, j_max, target_salary_min, target_salary_max):
                stats["drop_salary"] += 1
                continue
        # 城市
        if not _city_match(job.get("city_name") or "", target_cities):
            stats["drop_city"] += 1
            continue
        # 公司黑名单
        if not _company_not_blacklisted(job.get("company_type") or "", company_type_blacklist):
            stats["drop_blacklist"] += 1
            continue
        result.append(job)
        stats["pass"] += 1
    return result, stats


def _extract_json_array_from_text(text: str) -> list:
    """
    从 LLM 输出中提取 JSON 数组。兼容多种格式：
    - 标准数组 [{...}, {...}]
    - 多个独立对象 {...}{...} 或 {...}\n{...}
    """
    if not text or not isinstance(text, str):
        raise ValueError("输入为空")
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()

    # 1. 尝试解析为数组
    arr_start = text.find("[")
    arr_end = text.rfind("]")
    if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
        try:
            return json.loads(text[arr_start : arr_end + 1])
        except json.JSONDecodeError:
            pass

    # 2. 多个独立对象：用 JSONDecoder.raw_decode 逐个解析
    results = []
    decoder = json.JSONDecoder()
    idx = 0
    text = text.strip()
    while idx < len(text):
        # 跳过空白和逗号
        while idx < len(text) and text[idx] in " \t\n\r,[]":
            idx += 1
        if idx >= len(text):
            break
        if text[idx] != "{":
            idx += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, idx)
            results.append(obj)
            idx += end
        except json.JSONDecodeError:
            idx += 1
    if not results:
        raise ValueError("未找到有效的 JSON 对象")
    return results


def _call_llm_match_batch(
    jobs: List[Dict],
    profile: Dict,
    model_id: str,
) -> Tuple[List[Dict], Dict]:
    """
    对一批岗位调用 LLM 评分，返回 (results, token_info)
    results: 每条对应一个 job，格式见输出 schema
    """
    api_key, base_url, model_name = _get_model_config(model_id)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120)

    profile_str = json.dumps(profile, ensure_ascii=False, indent=2)
    jobs_input = []
    for j in jobs:
        jobs_input.append({
            "job_id": j.get("job_id"),
            "job_name": j.get("job_name"),
            "company_name": j.get("company_name"),
            "salary_desc": j.get("salary_desc"),
            "job_desc": (j.get("job_desc") or "")[:4000],
            "company_type": j.get("company_type"),
        })

    prompt = f"""你是一个求职匹配分析助手。根据候选人的简历 profile 和岗位 JD，对每条岗位给出匹配评分。

【评分标准】（四维分数与综合分请严格按下列锚点评分）

skill_match（技能匹配）：
90-100：JD要求的核心技能简历都有且熟练
70-89：大部分技能对口，个别技能有相关但不完全对口的经验
60-69：只匹配部分技能，多个核心技能缺失
60以下：技能栈基本不对口

experience_match（经验匹配）：
90-100：年限符合，行业和项目经历直接对口
70-89：年限接近，有可迁移的项目经验
60-69：年限或行业经验有明显差距
60以下：经验完全不匹配

growth_potential（成长空间）：
90-100：岗位方向与候选人发展路径高度一致，能接触核心技术或更大业务规模
70-89：方向基本一致，有一定成长空间
60-69：成长空间有限，岗位偏执行或重复性工作
60以下：与职业发展方向不符

culture_fit（文化契合）：
90-100：公司类型、赛道、工作方式完全符合求职偏好
70-89：大部分偏好匹配，个别方面不完全理想
60-69：有明显不符合偏好的地方
60以下：命中偏好中的排除项

match_score：四个维度的综合判断，不是简单平均；技能与经验权重更高。

【输出格式】必须只输出严格可解析的 JSON 数组，不要任何其他文字。每条岗位对应一个对象：
{{
  "match_score": 0-100 的综合匹配分,
  "dimension_scores": {{
    "skill_match": 0-100 技能匹配度,
    "experience_match": 0-100 经验匹配度,
    "growth_potential": 0-100 成长潜力匹配,
    "culture_fit": 0-100 文化/偏好契合度
  }},
  "gaps": ["差距1", "差距2"]
}}

【简历 profile】
{profile_str}

【待评分岗位】（共 {len(jobs_input)} 条）
{json.dumps(jobs_input, ensure_ascii=False, indent=2)}

请输出 JSON 数组，长度必须为 {len(jobs_input)}，顺序与输入一致。"""

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": "你只输出严格可解析的 JSON 数组，不要解释、markdown 或多余文字。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content.strip()
    token_info = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if hasattr(response, "usage") and response.usage:
        token_info["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0) or 0
        token_info["completion_tokens"] = getattr(response.usage, "completion_tokens", 0) or 0
        token_info["total_tokens"] = getattr(response.usage, "total_tokens", 0) or 0

    data = _extract_json_array_from_text(content)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("LLM 输出不是数组")

    results = []
    for i, item in enumerate(data):
        job_id = jobs[i].get("job_id") if i < len(jobs) else ""
        ds = item.get("dimension_scores") or {}
        r = {
            "job_id": job_id,
            "match_score": int(item.get("match_score", 0)) if item.get("match_score") is not None else 0,
            "dimension_scores": {
                "skill_match": int(ds.get("skill_match", 0)) if ds.get("skill_match") is not None else 0,
                "experience_match": int(ds.get("experience_match", 0)) if ds.get("experience_match") is not None else 0,
                "growth_potential": int(ds.get("growth_potential", 0)) if ds.get("growth_potential") is not None else 0,
                "culture_fit": int(ds.get("culture_fit", 0)) if ds.get("culture_fit") is not None else 0,
            },
            "strengths": [],
            "gaps": item.get("gaps") if isinstance(item.get("gaps"), list) else [],
            "advice": "",
        }
        results.append(r)
    return results, token_info


def gaps_to_display_strings(gaps) -> List[str]:
    """
    将粗评或深度分析中的 gaps（字符串列表或结构化对象列表）转为展示用字符串列表。
    """
    if not gaps:
        return []
    out: List[str] = []
    for g in gaps:
        if isinstance(g, str):
            s = g.strip()
            if s:
                out.append(s)
        elif isinstance(g, dict):
            desc = (g.get("description") or "").strip()
            if desc:
                out.append(desc)
    return out
