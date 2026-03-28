"""
差距分析改写 Agent（单阶段）
- 差距来源：仅使用「简历匹配」阶段已写入数据库的 JSON（深度匹配 submit_match_result 或粗评 gaps 合成），不再单独调用 LLM 做差距判断。
- Agent 2：根据上述差距上下文生成改写建议 + 四维评估（Function Calling：submit_rewrite_result）。
"""
import re
import json
import logging
from typing import Dict, List, Optional, Tuple

from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
logger = logging.getLogger(__name__)


def _get_model_config(model_id: str) -> Tuple[str, str, str]:
    configs = {
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
    cfg = configs.get(model_id) or configs.get("deepseek_chat")
    if not cfg or not all(cfg):
        raise ValueError("请在 .env 中配置 DEEPSEEK_API_KEY 和 DEEPSEEK_BASE_URL")
    return cfg


def parse_years_from_jd(jd_text: str) -> Optional[Tuple[int, int]]:
    """
    从 JD 文本解析工作经验年限要求。
    返回 (min_years, max_years)，如 (1, 3) 表示 1-3 年；无法解析返回 None。
    """
    if not jd_text or not isinstance(jd_text, str):
        return None
    s = jd_text.strip()
    # 1-3年、3-5年、1年以上、3年以下、1年经验、经验不限
    m = re.search(r"(\d+)\s*[-~至到]\s*(\d+)\s*年", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d+)\s*年\s*以上", s)
    if m:
        return (int(m.group(1)), 99)
    m = re.search(r"(\d+)\s*年\s*以下", s)
    if m:
        return (0, int(m.group(1)))
    m = re.search(r"(\d+)\s*年\s*经验", s)
    if m:
        y = int(m.group(1))
        return (y, y)
    m = re.search(r"(\d+)\s*年\s*及\s*以上", s)
    if m:
        return (int(m.group(1)), 99)
    return None


# Agent 2 工具定义（差距上下文来自匹配阶段入库的 JSON，见 build_rewrite_context_from_match_row）
AGENT2_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_rewrite_result",
            "description": "提交改写建议和四维评估",
            "parameters": {
                "type": "object",
                "properties": {
                    "rewrites": {
                        "type": "array",
                        "description": "按板块改写；顺序须为：自我评价→工作经历→项目经历→技能→教育背景；工作经历与项目经历须分条，勿合并标题",
                        "items": {
                            "type": "object",
                            "properties": {
                                "section": {
                                    "type": "string",
                                    "description": "板块名：自我评价、工作经历、项目经历、技能、教育背景之一；勿用「工作经历/项目经验」合并",
                                },
                                "original": {"type": "string"},
                                "rewritten": {"type": "string"},
                                "change_note": {"type": "string"},
                                "source": {
                                    "type": "string",
                                    "enum": ["原文改写", "合理延伸"],
                                    "description": "依据来源：原文改写=基于简历原文措辞升级/关键词对齐；合理延伸=补充候选人合理具备但简历未明确写出的能力",
                                },
                            },
                            "required": ["section", "original", "rewritten", "change_note", "source"],
                        },
                    },
                    "eval_before": {
                        "type": "object",
                        "description": "改前四维评分",
                        "properties": {
                            "skill_match": {"type": "integer"},
                            "experience_match": {"type": "integer"},
                            "growth_potential": {"type": "integer"},
                            "culture_fit": {"type": "integer"},
                        },
                    },
                    "eval_after": {
                        "type": "object",
                        "description": "改后预期四维评分",
                        "properties": {
                            "skill_match": {"type": "integer"},
                            "experience_match": {"type": "integer"},
                            "growth_potential": {"type": "integer"},
                            "culture_fit": {"type": "integer"},
                        },
                    },
                },
                "required": ["rewrites", "eval_before", "eval_after"],
            },
        },
    }
]


def run_agent2(
    profile: Dict,
    job: Dict,
    gap_context: Dict,
    model_id: str = "deepseek_chat",
) -> Tuple[Dict, Dict]:
    """
    改写 + 评估（年限硬伤时只返回空改写 + 评估说明）。

    gap_context：与旧版「Agent1 提交」同结构（years_verdict、gap_items、materials），
    由 match_results 中的 JSON 转换而来，见 build_rewrite_context_from_match_row。
    Returns: (result_dict, token_info)
    """
    api_key, base_url, model_name = _get_model_config(model_id)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120)

    profile_str = json.dumps(profile, ensure_ascii=False, indent=2)
    jobs_input = {
        "job_name": job.get("job_name"),
        "company_name": job.get("company_name"),
        "job_desc": (job.get("job_desc") or "")[:6000],
    }

    years_verdict = gap_context.get("years_verdict", {})
    is_hard = years_verdict.get("is_hard_injury", False)

    system = """你是简历改写专家。根据差距分析结果，按板块生成具体改写建议。

**板块与顺序（必须严格遵守）**：
- `rewrites` 中每条 `section` 须使用固定名称之一，且**整体顺序**为：**自我评价 → 工作经历 → 项目经历 → 技能 → 教育背景**（无内容可省略该条，但不要打乱其余条目的相对顺序）。
- **「工作经历」与「项目经历」必须分开**：工作经历只写任职/公司内职责与成果；项目经历只写独立项目或项目制交付。**禁止**使用「工作经历/项目经验」等合并标题。
- 其他板块名称用：「自我评价」「技能」「教育背景」。

改写规则：
1. 可以对简历已有经历进行措辞升级和关键词对齐；
2. 可以补充候选人合理具备但简历未明确写出的能力；
3. 不得编造简历中完全没有依据的技术经验或量化数据。

每条改写必须标注 source：
- 原文改写：基于简历原文的措辞升级、关键词对齐；
- 合理延伸：补充候选人合理具备但简历未明确写出的能力，让用户自行判断是否采用。

输出格式：板块 → 原文 → 改写后 → 改动说明 → 依据来源(source)。
同时给出改前、改后的四维评分（skill_match, experience_match, growth_potential, culture_fit），0-100。"""

    if is_hard:
        system += "\n\n注意：年限为硬伤，不生成改写建议，rewrites 为空数组，eval_before 和 eval_after 可相同，reason 中说明年限硬伤。"

    user = f"""【简历 profile】
{profile_str}

【岗位 JD】
{json.dumps(jobs_input, ensure_ascii=False, indent=2)}

【匹配阶段差距分析（JSON，已持久化；含年限判定、差距项、素材线索）】
{json.dumps(gap_context, ensure_ascii=False, indent=2)}

请调用 submit_rewrite_result 提交结果。"""

    logger.info("[Agent 2] 开始 API 调用（改写建议与评估）")
    resp = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=AGENT2_TOOLS,
        tool_choice={"type": "function", "function": {"name": "submit_rewrite_result"}},
        temperature=0.2,
    )
    logger.info("[Agent 2] API 调用返回，解析工具调用 submit_rewrite_result")

    token_info = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if hasattr(resp, "usage") and resp.usage:
        token_info["prompt_tokens"] = getattr(resp.usage, "prompt_tokens", 0) or 0
        token_info["completion_tokens"] = getattr(resp.usage, "completion_tokens", 0) or 0
        token_info["total_tokens"] = getattr(resp.usage, "total_tokens", 0) or 0

    result = {}
    for msg in resp.choices or []:
        tc = getattr(msg, "message", None)
        if not tc:
            continue
        tool_calls = getattr(tc, "tool_calls", None) or []
        for t in tool_calls:
            fn = getattr(t, "function", None)
            if fn and getattr(fn, "name", "") == "submit_rewrite_result":
                args = getattr(fn, "arguments", "")
                if args:
                    try:
                        result = json.loads(args)
                        break
                    except json.JSONDecodeError:
                        pass

    if not result:
        raise ValueError("Agent 2 未返回有效结果")
    return result, token_info


# 共性驱动「主简历」改写：与单岗 job_id 区分，落库 agent_analysis 时使用此占位 ID
COMMONALITY_MASTER_JOB_ID = "__COMMONALITY_MASTER__"


def run_agent2_master_from_commonality(
    profile: Dict,
    commonality_report: Dict,
    model_id: str = "deepseek_chat",
) -> Tuple[Dict, Dict]:
    """
    基于 Top 深度匹配岗位共性报告，产出一版可覆盖这批岗位共同要求的主简历改写建议（非逐岗 JD）。
    仍使用 submit_rewrite_result schema。
    """
    from match_analyzer import _get_model_config

    api_key, base_url, model_name = _get_model_config(model_id)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=180.0)

    profile_str = json.dumps(profile, ensure_ascii=False, indent=2)
    report_str = json.dumps(commonality_report, ensure_ascii=False, indent=2)

    system = """你是简历改写专家。输入为「头部深度匹配岗位的共性分析报告」与候选人简历 profile，**不是**某一家公司的单条 JD。

**目标**：产出一版**主简历**的改写建议，使经历与技能表述能**对齐**报告中的共同要求、高频 ATS 关键词与优先差距；同一版简历可用于投递这批目标岗位中的多个岗位，**无需**为每个岗位单独改一版。个别岗位若仍有特殊关键词，用户可自行微调。

**板块与顺序（必须严格遵守）**：
- `rewrites` 中每条 `section` 须使用固定名称之一，且**整体顺序**为：**自我评价 → 工作经历 → 项目经历 → 技能 → 教育背景**（无内容可省略该条，但不要打乱其余条目的相对顺序）。
- **「工作经历」与「项目经历」必须分开**：工作经历只写任职/公司内职责与成果；项目经历只写独立项目或项目制交付。**禁止**使用「工作经历/项目经验」等合并标题。
- 其他板块名称用：「自我评价」「技能」「教育背景」。

**改写规则**：
1. 按上述板块给出具体改写建议；
2. 每条必须标注 source：原文改写 = 基于简历原文的措辞升级与关键词对齐；合理延伸 = 简历未写明但候选人合理具备的能力，由用户自行判断是否采用；
3. 不得编造简历中完全没有依据的技术经验或量化数据；
4. 将共性报告中的「优先差距」「简历优化方向」「ATS 相关词」落实到具体模块与句子级建议。

**四维评估**：eval_before 表示当前简历相对「这批岗位共性期望」的综合匹配感；eval_after 表示按上述主简历改写后的预期四维（0-100）。"""

    user = f"""【简历 profile】
{profile_str}

【共性分析报告】（来自多份深度匹配结果的横向归纳，无 JD 原文）
{report_str}

请调用 submit_rewrite_result 提交主简历改写建议与改前/改后四维评估。"""

    logger.info("[Agent 2 主简历] 开始 API 调用（共性驱动）")
    resp = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=AGENT2_TOOLS,
        tool_choice={"type": "function", "function": {"name": "submit_rewrite_result"}},
        temperature=0.2,
    )

    token_info = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if hasattr(resp, "usage") and resp.usage:
        token_info["prompt_tokens"] = getattr(resp.usage, "prompt_tokens", 0) or 0
        token_info["completion_tokens"] = getattr(resp.usage, "completion_tokens", 0) or 0
        token_info["total_tokens"] = getattr(resp.usage, "total_tokens", 0) or 0

    result: Dict = {}
    for msg in resp.choices or []:
        tc = getattr(msg, "message", None)
        if not tc:
            continue
        tool_calls = getattr(tc, "tool_calls", None) or []
        for t in tool_calls:
            fn = getattr(t, "function", None)
            if fn and getattr(fn, "name", "") == "submit_rewrite_result":
                args = getattr(fn, "arguments", "")
                if args:
                    try:
                        result = json.loads(args)
                        break
                    except json.JSONDecodeError:
                        pass

    if not result:
        raise ValueError("主简历改写 Agent 未返回有效结果")
    return result, token_info


def rewrite_context_from_match_analysis(
    gap_analysis: Dict,
    profile: Optional[Dict] = None,
    job: Optional[Dict] = None,
) -> Dict:
    """
    将匹配阶段保存的 JSON（深度 Match Agent 的 submit_match_result 形态，或仅含 gaps 列表的简化对象）
    转为「改写 Agent」所需的 gap_context（years_verdict、gap_items、materials）。
    """
    profile = profile or {}
    job = job or {}
    gaps_in = gap_analysis.get("gaps") or []
    gap_items: List[Dict] = []
    materials: List[Dict] = []

    for g in gaps_in:
        if isinstance(g, str):
            gap_items.append(
                {
                    "type": "可包装",
                    "dimension": "综合",
                    "description": g,
                    "jd_keywords": [],
                    "resume_materials": [],
                }
            )
            continue
        if not isinstance(g, dict):
            continue
        t = g.get("type") or "可包装"
        if t not in ("硬伤", "可包装"):
            t = "硬伤" if ("硬" in str(t) or str(t).lower() == "hard") else "可包装"
        gi = {
            "type": t,
            "dimension": g.get("dimension") or "综合",
            "description": g.get("description") or "",
            "jd_keywords": g.get("jd_keywords") if isinstance(g.get("jd_keywords"), list) else [],
            "resume_materials": g.get("resume_materials")
            if isinstance(g.get("resume_materials"), list)
            else [],
        }
        gap_items.append(gi)
        for m in gi.get("resume_materials") or []:
            if m:
                materials.append(
                    {
                        "section": "简历",
                        "content": str(m),
                        "usable_for": "",
                    }
                )

    yv = gap_analysis.get("years_verdict")
    if not isinstance(yv, dict) or not yv:
        jd_range = parse_years_from_jd(job.get("job_desc") or "")
        jd_min, jd_max = (jd_range if jd_range else (0, 0))
        hard_year = any(
            (x.get("type") == "硬伤" and "年" in str(x.get("dimension", "")))
            for x in gap_items
        )
        yv = {
            "jd_min_years": jd_min,
            "jd_max_years": jd_max,
            "resume_years": 0,
            "is_hard_injury": bool(hard_year),
            "reason": "由匹配深度分析推断年限；建议以 Agent2 评估为准。",
        }
    else:
        yv = {
            "jd_min_years": int(yv.get("jd_min_years", 0) or 0),
            "jd_max_years": int(yv.get("jd_max_years", 0) or 0),
            "resume_years": int(yv.get("resume_years", 0) or 0),
            "is_hard_injury": bool(yv.get("is_hard_injury", False)),
            "reason": str(yv.get("reason") or ""),
        }

    return {
        "years_verdict": yv,
        "gap_items": gap_items,
        "materials": materials,
    }


def build_rewrite_context_from_match_row(
    mr: Dict,
    profile: Optional[Dict] = None,
    job: Optional[Dict] = None,
) -> Dict:
    """
    从 db.get_match_result_row 的一行构造 gap_context。

    - 若存在 gap_analysis_json（深度匹配写入的 JSON 字符串）：解析为 dict 后走 rewrite_context_from_match_analysis。
    - 否则使用粗评字段：gaps（字符串列表）等合成简化 gap_analysis，再走同一转换逻辑。

    说明：数据存于 SQLite 的 TEXT 列，不是独立 .json 文件；运行时解析为 dict 传给 Agent2。
    """
    profile = profile or {}
    job = job or {}
    raw = mr.get("gap_analysis_json")
    parsed: Optional[Dict] = None
    if raw:
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
        elif isinstance(raw, dict):
            parsed = raw
    if parsed:
        return rewrite_context_from_match_analysis(parsed, profile, job)

    gaps = mr.get("gaps") or []
    if isinstance(gaps, str):
        try:
            gaps = json.loads(gaps) if gaps else []
        except Exception:
            gaps = []
    synthetic: Dict = {"gaps": gaps if isinstance(gaps, list) else [], "years_verdict": {}}
    return rewrite_context_from_match_analysis(synthetic, profile, job)
