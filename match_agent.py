"""
深度匹配 Agent：默认单次 Function Calling 提交 submit_match_result；
可选 MATCH_AGENT_MODE=loop 走多轮工具循环（便于后续接 RAG/外部 API）。
仅对粗评 match_score >= HEAVY_THRESHOLD 的岗位调用。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from match_analyzer import _get_model_config

load_dotenv()
logger = logging.getLogger(__name__)

HEAVY_THRESHOLD = 80
MATCH_AGENT_VERSION = "match_agent_v1"
COARSE_AGENT_VERSION = "coarse_v1"

# 典型链路约 5 轮（detail→profile→jd→verify→submit），留余量防偶发多一轮思考；过高易在异常循环时烧 token
MAX_AGENT_ROUNDS = 10

# 单次 chat.completions 在「多轮 tools + 长上下文」下，服务端分块传输偶发 incomplete chunked read；拉长 read 超时并由应用层重试
_MATCH_AGENT_READ_TIMEOUT = float(os.getenv("MATCH_AGENT_HTTP_READ_TIMEOUT", "420"))
_MATCH_AGENT_CONNECT_TIMEOUT = float(os.getenv("MATCH_AGENT_HTTP_CONNECT_TIMEOUT", "60"))
# SDK 内置重试与下方 _chat_completion_with_retry 二选一易重复计数，此处关闭 SDK 层重试，统一走应用层
_MATCH_AGENT_SDK_MAX_RETRIES = int(os.getenv("MATCH_AGENT_OPENAI_SDK_MAX_RETRIES", "0"))
_MATCH_AGENT_COMPLETION_RETRIES = int(os.getenv("MATCH_AGENT_COMPLETION_RETRIES", "6"))
# 每完成一轮「模型回复 + 工具执行」后，下一轮请求前暂停，减轻网关限流/断流（秒，0 表示不间隔）
_MATCH_AGENT_ROUND_DELAY_SEC = float(os.getenv("MATCH_AGENT_ROUND_DELAY_SEC", "1.0"))
# 发往 LLM 时保留最近 N 条 tool 消息全文，更早的 tool 返回压缩为摘要，降低最后一轮 submit 前上下文过长导致断流
_MATCH_AGENT_TOOL_COMPRESS_KEEP_LAST = int(os.getenv("MATCH_AGENT_TOOL_COMPRESS_KEEP_LAST", "8"))
_MATCH_AGENT_TOOL_COMPRESS_PREVIEW = int(os.getenv("MATCH_AGENT_TOOL_COMPRESS_PREVIEW", "480"))

# 深度匹配模式：single（默认，单次 submit）| loop（多轮工具循环）
_MATCH_AGENT_MODE_RAW = (os.getenv("MATCH_AGENT_MODE") or "single").strip().lower()
# 单次模式 user 消息中 JD 全文最大字符数（防超长上下文）
_MATCH_AGENT_SINGLE_MAX_JD_CHARS = int(os.getenv("MATCH_AGENT_SINGLE_MAX_JD_CHARS", "12000"))
# 后置校验失败后允许模型重试的次数（不含首次），默认 2 → 最多 3 次 API 调用
_MATCH_AGENT_POST_VALIDATE_MAX_RETRIES = int(os.getenv("MATCH_AGENT_POST_VALIDATE_MAX_RETRIES", "2"))


def _match_agent_mode() -> str:
    """返回 single 或 loop。"""
    if _MATCH_AGENT_MODE_RAW in ("loop", "multi", "agent"):
        return "loop"
    return "single"


def _messages_for_api_request(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    深拷贝 messages，并将较早的 tool 返回压缩为短摘要（仅影响请求体，不修改会话内存中的完整内容）。
    """
    cm = copy.deepcopy(messages)
    keep = max(1, _MATCH_AGENT_TOOL_COMPRESS_KEEP_LAST)
    tool_indices = [i for i, m in enumerate(cm) if m.get("role") == "tool"]
    if len(tool_indices) <= keep:
        return cm
    logger.info(
        "[深度匹配] 请求 LLM 前压缩 tool 历史：共 %d 条 tool，保留最近 %d 条全文",
        len(tool_indices),
        keep,
    )
    for idx in tool_indices[:-keep]:
        content = cm[idx].get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False) if content is not None else ""
        if len(content) <= _MATCH_AGENT_TOOL_COMPRESS_PREVIEW:
            continue
        prev = content.replace("\n", " ").strip()
        if len(prev) > _MATCH_AGENT_TOOL_COMPRESS_PREVIEW:
            prev = prev[: _MATCH_AGENT_TOOL_COMPRESS_PREVIEW - 1] + "…"
        cm[idx]["content"] = json.dumps(
            {
                "_compressed": True,
                "hint": "前序工具返回已压缩，仅保留摘要；分析结论已在当前对话上下文中。",
                "preview": prev,
                "original_chars": len(content),
            },
            ensure_ascii=False,
        )
    return cm


def _preview_for_log(s: str, max_len: int = 160) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _is_transient_llm_error(exc: BaseException, _depth: int = 0) -> bool:
    """网络中断、分块读不完整、限流等可重试。"""
    if _depth > 8:
        return False
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    name = type(exc).__name__
    if name in (
        "RemoteProtocolError",
        "ReadTimeout",
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "WriteError",
    ):
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_transient_llm_error(cause, _depth + 1)
    ctx = getattr(exc, "__context__", None)
    if ctx is not None and ctx is not cause:
        return _is_transient_llm_error(ctx, _depth + 1)
    return False


def _create_match_agent_client(api_key: str, base_url: str) -> OpenAI:
    timeout = httpx.Timeout(
        connect=_MATCH_AGENT_CONNECT_TIMEOUT,
        read=_MATCH_AGENT_READ_TIMEOUT,
        write=120.0,
        pool=60.0,
    )
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=_MATCH_AGENT_SDK_MAX_RETRIES,
    )


def _chat_completion_with_retry(client: OpenAI, **kwargs: Any):
    """对 chat.completions.create 做指数退避重试（应对 DeepSeek 等网关偶发断流）。"""
    max_attempts = max(1, _MATCH_AGENT_COMPLETION_RETRIES)
    last: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            last = e
            if not _is_transient_llm_error(e) or attempt >= max_attempts:
                raise
            delay = min(2.0 ** (attempt - 1), 45.0)
            logger.warning(
                "深度匹配 API 请求异常 %s: %s，%.1fs 后重试 (%d/%d)",
                type(e).__name__,
                e,
                delay,
                attempt,
                max_attempts,
            )
            time.sleep(delay)
    assert last is not None
    raise last

# submit_match_result 参数 schema（OpenAI tools）
_SUBMIT_PROPERTIES: Dict[str, Any] = {
    "match_score": {"type": "integer", "description": "0-100 综合匹配分"},
    "dimension_scores": {
        "type": "object",
        "properties": {
            "skill_match": {"type": "integer"},
            "experience_match": {"type": "integer"},
            "growth_potential": {"type": "integer"},
            "culture_fit": {"type": "integer"},
        },
        "required": ["skill_match", "experience_match", "growth_potential", "culture_fit"],
    },
    "gaps": {
        "type": "array",
        "description": "结构化差距项",
        "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["硬伤", "可包装"]},
                "severity": {"type": "string", "description": "高/中/低"},
                "dimension": {"type": "string"},
                "description": {"type": "string"},
                "jd_keywords": {"type": "array", "items": {"type": "string"}},
                "resume_materials": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "hidden_expectations": {"type": "string", "description": "JD 未写明但隐含的期望"},
    "ats_keywords": {
        "type": "array",
        "items": {"type": "string"},
        "description": "建议简历对齐的 ATS/关键词",
    },
    "recommendation": {
        "type": "string",
        "enum": ["投递", "谨慎投递", "可以试但概率低", "不建议投递"],
        "description": "投递建议四档",
    },
    "strengths": {"type": "array", "items": {"type": "string"}},
    "years_verdict": {
        "type": "object",
        "properties": {
            "jd_min_years": {"type": "integer"},
            "jd_max_years": {"type": "integer"},
            "resume_years": {"type": "integer"},
            "is_hard_injury": {"type": "boolean"},
            "reason": {"type": "string"},
        },
    },
}

MATCH_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_job_detail",
            "description": "获取当前岗位的已加载信息（职位名、公司、薪资、JD 摘要等），无需联网。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": "获取候选人简历 profile（已加载），用于对照 JD。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deep_analyze_jd",
            "description": "对当前岗位 JD 做要点摘录（基于已加载文本，不重复爬取）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": "可选：技能/年限/业务/团队 等关注点",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_score",
            "description": (
                "自检草稿：除 0–100 范围外，校验综合分与四维均值/加权均值是否一致、"
                "与 gaps（硬伤+严重度）及 recommendation 是否矛盾。draft 请尽量含 gaps、recommendation 以便完整校验。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "draft": {
                        "type": "object",
                        "description": (
                            "match_score、dimension_scores 必填；可选 gaps（type/severity）、"
                            "recommendation（四档）以启用锚点与一致性校验"
                        ),
                    }
                },
                "required": ["draft"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_match_result",
            "description": "提交最终匹配分析结果（必须调用一次以结束任务）。",
            "parameters": {
                "type": "object",
                "properties": _SUBMIT_PROPERTIES,
                "required": [
                    "match_score",
                    "dimension_scores",
                    "gaps",
                    "hidden_expectations",
                    "ats_keywords",
                    "recommendation",
                ],
            },
        },
    },
]

# 仅 submit_match_result，用于单次模式强制 tool_choice
MATCH_AGENT_SUBMIT_ONLY_TOOLS = [MATCH_AGENT_TOOLS[-1]]


def _severity_is_high(sev: str) -> bool:
    s = (sev or "").strip().lower()
    if not s:
        return False
    if s in ("高", "严重", "极高", "致命"):
        return True
    if s in ("high", "critical", "severe"):
        return True
    return "高" in (sev or "") and "中" not in (sev or "")


def _gap_type_is_hard(g: Dict[str, Any]) -> bool:
    t = str(g.get("type") or "").strip()
    return t == "硬伤" or ("硬伤" in t and "可包装" not in t)


def _verify_score_draft(draft: Any) -> Dict[str, Any]:
    """
    锚点校验：综合分 vs 四维、vs 加权四维、vs gaps（硬伤+高严重度）、vs recommendation。
    返回 valid / notes / checks 供工具 JSON 序列化。
    """
    notes: List[str] = []
    checks: Dict[str, bool] = {
        "numeric_range": True,
        "match_vs_dimensions": True,
        "match_vs_weighted_dimensions": True,
        "match_vs_gaps": True,
        "recommendation_vs_score": True,
        "recommendation_vs_gaps": True,
    }
    if not isinstance(draft, dict):
        return {
            "valid": False,
            "notes": ["draft 必须是对象"],
            "checks": {k: False for k in checks},
        }

    ms_raw = draft.get("match_score")
    ds = draft.get("dimension_scores") or {}
    gaps = draft.get("gaps")
    if gaps is None:
        gaps = []
    if not isinstance(gaps, list):
        gaps = []
    rec = draft.get("recommendation")
    rec_str = str(rec).strip() if rec is not None else ""

    msv: Optional[int] = None
    try:
        if ms_raw is not None:
            msv = int(ms_raw)
    except (TypeError, ValueError):
        msv = None

    dim_keys = ("skill_match", "experience_match", "growth_potential", "culture_fit")
    dim_vals: List[int] = []
    for k in dim_keys:
        v = ds.get(k)
        try:
            if v is not None:
                dim_vals.append(int(v))
        except (TypeError, ValueError):
            checks["numeric_range"] = False
            notes.append(f"{k} 无法解析为整数")

    # —— 范围 ——
    if msv is not None:
        if msv < 0 or msv > 100:
            checks["numeric_range"] = False
            notes.append("match_score 应在 0–100")
    else:
        checks["numeric_range"] = False
        notes.append("缺少或非法的 match_score")

    for k in dim_keys:
        v = ds.get(k)
        try:
            if v is None:
                checks["numeric_range"] = False
                notes.append(f"缺少 {k}")
            else:
                iv = int(v)
                if iv < 0 or iv > 100:
                    checks["numeric_range"] = False
                    notes.append(f"{k} 应在 0–100")
        except (TypeError, ValueError):
            checks["numeric_range"] = False
            notes.append(f"{k} 无法解析为整数")

    # —— 综合分 vs 四维均值 / 加权（技能、经验权重更高，与粗评锚点一致）——
    if len(dim_vals) == 4 and msv is not None:
        avg = sum(dim_vals) / 4.0
        w_avg = (
            0.35 * dim_vals[0]
            + 0.35 * dim_vals[1]
            + 0.15 * dim_vals[2]
            + 0.15 * dim_vals[3]
        )
        diff_avg = abs(msv - avg)
        diff_w = abs(msv - w_avg)
        if diff_avg > 12:
            checks["match_vs_dimensions"] = False
            notes.append(
                f"综合分({msv})与四维算术均值({avg:.1f})差距>{12}分，需说明综合判断依据或调整分数"
            )
        if diff_w > 14:
            checks["match_vs_weighted_dimensions"] = False
            notes.append(
                f"综合分({msv})与加权均值(技能/经验各35%，成长/契合各15%)={w_avg:.1f}差距>{14}分，不一致"
            )
        if min(dim_vals) >= 80 and msv < 72:
            checks["match_vs_dimensions"] = False
            notes.append(
                "四维均≥80但综合分<72：与「各维度表现都很好」矛盾，请提高综合分或下调部分维度"
            )
        if max(dim_vals) <= 68 and msv >= 78:
            checks["match_vs_dimensions"] = False
            notes.append(
                "四维均≤68但综合分≥78：综合分偏高，与四维偏低不一致"
            )
        spread = max(dim_vals) - min(dim_vals)
        if spread > 28 and (msv < min(dim_vals) + 3 or msv > max(dim_vals) - 3):
            checks["match_vs_dimensions"] = False
            notes.append(
                f"四维极差较大(>{28})，综合分({msv})应落在维度区间附近并解释短板影响"
            )

    # —— gaps：硬伤 + 高严重度 vs 高分 ——
    hard_n = 0
    hard_high_sev = 0
    for g in gaps:
        if not isinstance(g, dict):
            continue
        if _gap_type_is_hard(g):
            hard_n += 1
            sev = str(g.get("severity") or "").strip()
            if _severity_is_high(sev):
                hard_high_sev += 1

    if msv is not None:
        if hard_high_sev >= 2 and msv >= 88:
            checks["match_vs_gaps"] = False
            notes.append(
                f"综合分≥88但 gaps 中有≥2条「硬伤+高严重度」，高分与硬伤矛盾，应降分或下调维度/改写 gaps 严重度"
            )
        elif hard_high_sev >= 2 and msv >= 85:
            checks["match_vs_gaps"] = False
            notes.append(
                "综合分≥85且存在≥2条高严重度硬伤：不合理，请下调综合分或重新评估硬伤"
            )
        elif hard_high_sev >= 1 and msv >= 92:
            checks["match_vs_gaps"] = False
            notes.append(
                "综合分≥92但存在高严重度硬伤：与「接近满分匹配」矛盾"
            )
        if hard_n >= 3 and msv >= 82 and hard_high_sev >= 1:
            checks["match_vs_gaps"] = False
            notes.append(
                "≥3条硬伤且其中含高严重度，综合分仍≥82：需整体降分或合并/降级差距表述"
            )

    # —— recommendation 与综合分 ——
    if msv is not None and rec_str:
        if rec_str == "投递" and msv < 76:
            checks["recommendation_vs_score"] = False
            notes.append("推荐档为「投递」但综合分<76，与四档惯例不一致（通常≥76~80）")
        if rec_str == "不建议投递" and msv >= 72:
            checks["recommendation_vs_score"] = False
            notes.append("推荐档为「不建议投递」但综合分≥72，与「明显不匹配」矛盾")
        if rec_str == "谨慎投递" and msv >= 92:
            checks["recommendation_vs_score"] = False
            notes.append("推荐档为「谨慎投递」但综合分≥92，与「偏保守」矛盾，请提高推荐档或下调分数说明")
        if rec_str == "可以试但概率低" and msv >= 86:
            checks["recommendation_vs_score"] = False
            notes.append("推荐档为「可以试但概率低」但综合分≥86，与「概率低」矛盾，请对齐")

    # —— recommendation 与 gaps（硬伤+高严重度）——
    if rec_str == "投递" and hard_high_sev >= 1:
        checks["recommendation_vs_gaps"] = False
        notes.append(
            "推荐档为「投递」但 gaps 中存在「硬伤+高严重度」项：不合理，应降为「谨慎投递」或更低调档，或下调硬伤严重度/合并表述"
        )

    valid = all(checks.values())
    return {
        "valid": valid,
        "notes": notes if notes else ["各项锚点检查通过"],
        "checks": checks,
        "meta": {
            "dimension_avg": round(sum(dim_vals) / 4.0, 2) if len(dim_vals) == 4 else None,
            "hard_injury_count": hard_n,
            "hard_high_severity_count": hard_high_sev,
        },
    }


def _clip_jd_lines(jd: str, max_lines: int = 40) -> str:
    if not jd:
        return ""
    lines = jd.replace("\r\n", "\n").split("\n")
    return "\n".join(lines[:max_lines])


def _execute_tool(name: str, arguments: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    profile = ctx.get("profile") or {}
    job = ctx.get("job") or {}
    if name == "get_job_detail":
        payload = {
            "job_id": job.get("job_id"),
            "job_name": job.get("job_name"),
            "company_name": job.get("company_name"),
            "salary_desc": job.get("salary_desc"),
            "city_name": job.get("city_name"),
            "company_type": job.get("company_type"),
            "experience": job.get("experience"),
            "job_desc_excerpt": ((job.get("job_desc") or "")[:3500]),
        }
        return json.dumps(payload, ensure_ascii=False)
    if name == "get_user_profile":
        return json.dumps(profile, ensure_ascii=False)
    if name == "deep_analyze_jd":
        jd = job.get("job_desc") or ""
        focus = (arguments or {}).get("focus") or ""
        excerpt = _clip_jd_lines(jd, 50)
        bullets = []
        for line in excerpt.split("\n"):
            line = line.strip()
            if len(line) > 2 and re.search(
                r"要求|职责|必备|优先|经验|技能|熟悉|精通|负责", line
            ):
                bullets.append(line[:200])
        return json.dumps(
            {
                "focus": focus,
                "jd_length": len(jd),
                "excerpt": excerpt[:4000],
                "keyword_lines": bullets[:15],
            },
            ensure_ascii=False,
        )
    if name == "verify_score":
        draft = (arguments or {}).get("draft") or {}
        out = _verify_score_draft(draft)
        return json.dumps(out, ensure_ascii=False)
    return json.dumps({"error": "unknown_tool", "name": name}, ensure_ascii=False)


def _normalize_submit_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    ds = raw.get("dimension_scores") or {}
    out = {
        "match_score": int(raw.get("match_score", 0) or 0),
        "dimension_scores": {
            "skill_match": int(ds.get("skill_match", 0) or 0),
            "experience_match": int(ds.get("experience_match", 0) or 0),
            "growth_potential": int(ds.get("growth_potential", 0) or 0),
            "culture_fit": int(ds.get("culture_fit", 0) or 0),
        },
        "gaps": raw.get("gaps") if isinstance(raw.get("gaps"), list) else [],
        "hidden_expectations": str(raw.get("hidden_expectations") or ""),
        "ats_keywords": raw.get("ats_keywords") if isinstance(raw.get("ats_keywords"), list) else [],
        "recommendation": str(raw.get("recommendation") or "谨慎投递"),
        "strengths": raw.get("strengths") if isinstance(raw.get("strengths"), list) else [],
        "years_verdict": raw.get("years_verdict") if isinstance(raw.get("years_verdict"), dict) else {},
    }
    allowed_rec = {"投递", "谨慎投递", "可以试但概率低", "不建议投递"}
    if out["recommendation"] not in allowed_rec:
        out["recommendation"] = "谨慎投递"
    return out


def _apply_fallback_from_verify_draft(
    draft: Dict[str, Any],
    job_id_str: str,
    reason: str,
    detail: str = "",
) -> Dict[str, Any]:
    """将 verify_score(valid=true) 时传入的 draft 规范化为 submit 形态，并打兜底标记。"""
    sub = _normalize_submit_payload(draft)
    sub["_meta_fallback"] = {
        "source": reason,
        "detail": (detail or "")[:800],
    }
    logger.warning(
        "[深度匹配] job_id=%s 使用 verify_draft 兜底 | reason=%s | match_score=%s recommendation=%s",
        job_id_str,
        reason,
        sub.get("match_score"),
        sub.get("recommendation"),
    )
    return sub


def build_advice_from_submit(sub: Dict[str, Any]) -> str:
    """用于 match_results.advice 列展示"""
    parts: List[str] = []
    if sub.get("recommendation"):
        parts.append(f"【投递建议】{sub['recommendation']}")
    he = sub.get("hidden_expectations")
    if he:
        parts.append(f"【隐含期望】{he}")
    ak = sub.get("ats_keywords") or []
    if ak:
        parts.append("【ATS/关键词】" + "、".join(str(x) for x in ak[:30]))
    return "\n".join(parts)


def _assistant_message_to_dict(msg) -> Dict[str, Any]:
    d: Dict[str, Any] = {"role": "assistant", "content": msg.content}
    tcs = getattr(msg, "tool_calls", None) or []
    if tcs:
        d["tool_calls"] = []
        for tc in tcs:
            fn = getattr(tc, "function", None)
            d["tool_calls"].append(
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": getattr(fn, "name", "") if fn else "",
                        "arguments": getattr(fn, "arguments", "") if fn else "",
                    },
                }
            )
    return d


def _build_single_match_system_prompt() -> str:
    """单次深度匹配：合并原多轮工具中的锚点、隐性要求、四档规则等到一条 system。"""
    return """你是资深求职匹配顾问。你将一次性收到岗位 JD 全文、候选人简历、求职偏好与粗评参考。
你必须且只能通过工具 **submit_match_result** 输出最终结构化结果（不要输出可读的总结性长文，除非无法完成工具调用）。

【四维评分锚点】（0–100，与粗评一致）
skill_match（技能匹配）：
90-100：JD 要求的核心技能简历都有且熟练
70-89：大部分技能对口，个别技能有相关但不完全对口的经验
60-69：只匹配部分技能，多个核心技能缺失
60 以下：技能栈基本不对口

experience_match（经验匹配）：
90-100：年限符合，行业和项目经历直接对口
70-89：年限接近，有可迁移的项目经验
60-69：年限或行业经验有明显差距
60 以下：经验完全不匹配

growth_potential（成长空间）：
90-100：岗位方向与候选人发展路径高度一致，能接触核心技术或更大业务规模
70-89：方向基本一致，有一定成长空间
60-69：成长空间有限，岗位偏执行或重复性工作
60 以下：与职业发展方向不符

culture_fit（文化/偏好契合）：
90-100：公司类型、赛道、工作方式完全符合求职偏好
70-89：大部分偏好匹配，个别方面不完全理想
60-69：有明显不符合偏好的地方
60 以下：命中偏好中的排除项

【综合分 match_score】
四个维度的综合判断，不是简单算术平均；技能与经验权重更高（可近似按 技能35% + 经验35% + 成长15% + 契合15% 自检），并与四维、硬伤、gaps、推荐档一致。

【隐性期望 hidden_expectations】
分析 JD 未写明但行业/岗位常见的隐含期望（如：加班强度、汇报线、数据规模、合规背景、英文读写、驻场/出差等），简要写出对候选人的隐含门槛。

【筛选门槛 vs 核心能力】
- 筛选门槛：学历、年限、证书、特定技术栈等「不满足则难进面」的条目；若候选人明显不满足，应在 gaps 标为硬伤并拉低相关维度/综合分。
- 核心能力：入职后能否胜任工作；二者区分清楚，避免把「优先」写成「必须」 unless JD 明确。

【gaps 与硬伤】
每项需 type=硬伤 或 可包装；severity 高/中/低；尽量给出 jd_keywords 与 resume_materials。不要编造简历中不存在的经历。

【投递建议四档】（recommendation 只能是其一）
- 投递：综合匹配高、硬伤少或仅为可包装；通常综合分不宜明显低于粗评合理区间，且不应在存在「硬伤+高严重度」时仍给本档。
- 谨慎投递：整体尚可但有关键短板或不确定性，需要改简历/补材料后再投。
- 可以试但概率低：差距明显，仅当候选人强烈意愿或岗位稀缺时可试。
- 不建议投递：硬性不符或匹配度过低。

【系统后置校验】
提交后系统会校验综合分与四维均值/加权均值是否一致、综合分与 gaps（硬伤+高严重度）及 recommendation 是否矛盾。若不一致，你会收到 issues 列表并需修正后再次仅调用 submit_match_result。"""


def _build_single_match_user_message(
    profile: Dict[str, Any],
    job: Dict[str, Any],
    coarse_hint: Optional[Dict[str, Any]],
    preferences: Dict[str, Any],
) -> str:
    jd = job.get("job_desc") or ""
    if len(jd) > _MATCH_AGENT_SINGLE_MAX_JD_CHARS:
        jd = jd[: max(0, _MATCH_AGENT_SINGLE_MAX_JD_CHARS - 3)] + "..."
    hint_str = json.dumps(coarse_hint or {}, ensure_ascii=False, indent=2)
    prefs_str = json.dumps(preferences or {}, ensure_ascii=False, indent=2)
    meta = {
        "job_id": job.get("job_id"),
        "job_name": job.get("job_name"),
        "company_name": job.get("company_name"),
        "salary_desc": job.get("salary_desc"),
        "city_name": job.get("city_name"),
        "experience": job.get("experience"),
        "company_type": job.get("company_type"),
    }
    profile_str = json.dumps(profile, ensure_ascii=False, indent=2)
    return f"""【粗评参考】（轻量模型批量结果，请深化或修正，可纠正与 JD/简历不一致处）
{hint_str}

【求职偏好】（城市、薪资、公司类型黑名单等，用于 culture_fit 与隐性门槛）
{prefs_str}

【岗位元信息】
{json.dumps(meta, ensure_ascii=False, indent=2)}

【岗位 JD 全文】
{jd}

【候选人简历 profile】
{profile_str}

任务：完成深度匹配分析，**仅调用一次** submit_match_result 提交完整结果（字段与 schema 一致）。"""


def run_match_agent_single(
    profile: Dict[str, Any],
    job: Dict[str, Any],
    model_id: str,
    coarse_hint: Optional[Dict[str, Any]] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    单次 LLM 调用 + submit_match_result；后置校验不通过时追加 user 消息重试（最多额外 MATCH_AGENT_POST_VALIDATE_MAX_RETRIES 次）。
    """
    api_key, base_url, model_name = _get_model_config(model_id)
    client = _create_match_agent_client(api_key, base_url)
    prefs = preferences or {}
    job_id_str = str((job or {}).get("job_id") or "?")

    system = _build_single_match_system_prompt()
    user_content = _build_single_match_user_message(profile, job, coarse_hint, prefs)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    total_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    max_attempts = 1 + max(0, _MATCH_AGENT_POST_VALIDATE_MAX_RETRIES)
    tool_choice = {"type": "function", "function": {"name": "submit_match_result"}}

    logger.info(
        "[深度匹配/single] job_id=%s 开始 | 模型=%s | 后置校验最多尝试 %d 次（含首次）",
        job_id_str,
        model_name,
        max_attempts,
    )

    for attempt in range(max_attempts):
        resp = _chat_completion_with_retry(
            client,
            model=model_name,
            messages=messages,
            tools=MATCH_AGENT_SUBMIT_ONLY_TOOLS,
            tool_choice=tool_choice,
            temperature=0.2,
        )
        if hasattr(resp, "usage") and resp.usage:
            total_tokens["prompt_tokens"] += getattr(resp.usage, "prompt_tokens", 0) or 0
            total_tokens["completion_tokens"] += getattr(resp.usage, "completion_tokens", 0) or 0
            total_tokens["total_tokens"] += getattr(resp.usage, "total_tokens", 0) or 0

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        submit_args: Optional[Dict[str, Any]] = None
        submit_tc = None
        json_error = False
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            if name == "submit_match_result":
                submit_tc = tc
                raw_args = getattr(fn, "arguments", "") if fn else "{}"
                try:
                    submit_args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError:
                    submit_args = None
                    json_error = True
                break

        if not tool_calls or submit_tc is None:
            if attempt >= max_attempts - 1:
                raise ValueError("单次深度匹配未调用 submit_match_result")
            messages.append(
                {
                    "role": "user",
                    "content": "你必须调用 submit_match_result 并传入完整参数，不要省略工具调用。",
                }
            )
            continue

        if json_error or not isinstance(submit_args, dict):
            if attempt >= max_attempts - 1:
                raise ValueError("单次深度匹配 submit_match_result 参数不是合法 JSON 对象")
            messages.append(_assistant_message_to_dict(msg))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": submit_tc.id,
                    "content": json.dumps(
                        {"error": "arguments_not_json_object", "hint": "请输出合法 JSON 作为 function arguments"},
                        ensure_ascii=False,
                    ),
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": "请修正参数格式，再次仅调用 submit_match_result。",
                }
            )
            continue

        normalized = _normalize_submit_payload(submit_args)
        vr = _verify_score_draft(normalized)
        if vr.get("valid"):
            normalized["_post_validate_attempts"] = attempt + 1
            logger.info(
                "[深度匹配/single] job_id=%s 成功 | 尝试=%d | match_score=%s recommendation=%s",
                job_id_str,
                attempt + 1,
                normalized.get("match_score"),
                normalized.get("recommendation"),
            )
            return normalized, total_tokens

        if attempt >= max_attempts - 1:
            notes = "; ".join(vr.get("notes") or [])
            raise ValueError(f"深度匹配后置校验未通过（已达最大尝试次数）: {notes}")

        messages.append(_assistant_message_to_dict(msg))
        if submit_tc is not None:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": submit_tc.id,
                    "content": json.dumps(
                        {
                            "validation_passed": False,
                            "issues": vr.get("notes", []),
                            "checks": vr.get("checks", {}),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        feedback = "\n".join(f"- {n}" for n in (vr.get("notes") or []))
        messages.append(
            {
                "role": "user",
                "content": (
                    "【系统后置校验未通过】请根据下列问题修正 match_score、dimension_scores、gaps 或 recommendation，"
                    "并再次**仅调用 submit_match_result** 提交修正后的完整结果：\n"
                    f"{feedback}"
                ),
            }
        )

    raise ValueError("单次深度匹配异常结束")


def run_match_agent_loop(
    profile: Dict[str, Any],
    job: Dict[str, Any],
    model_id: str,
    coarse_hint: Optional[Dict[str, Any]] = None,
    max_rounds: int = MAX_AGENT_ROUNDS,
    preferences: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    多轮工具循环，直到调用 submit_match_result。
    coarse_hint: 粗评结果（match_score, dimension_scores, gaps）供模型参考。
    preferences: 预留，与单次模式对齐；当前循环模式的 system user 未拼接偏好，可后续扩展。
    Returns: (normalized_submit_dict, token_info)
    """
    api_key, base_url, model_name = _get_model_config(model_id)
    client = _create_match_agent_client(api_key, base_url)

    ctx = {"profile": profile, "job": job}
    profile_str = json.dumps(profile, ensure_ascii=False, indent=2)
    hint_str = json.dumps(coarse_hint or {}, ensure_ascii=False, indent=2)

    system = """你是资深求职匹配顾问。请通过工具了解岗位与候选人信息，必要时做 JD 要点分析、校验分数草稿，最后**必须**调用 submit_match_result 提交完整结构化结果。
规则：
1. 先可用 get_job_detail / get_user_profile 建立上下文；需要时可 deep_analyze_jd。
2. 若需要自检分数，用 verify_score（draft 请含 match_score、dimension_scores，并尽量含 gaps、recommendation 以便校验综合分与四维/硬伤/推荐档是否一致）。
3. 最终必须调用 submit_match_result，recommendation 只能是四档之一：投递、谨慎投递、可以试但概率低、不建议投递。
4. gaps 中每项需区分硬伤/可包装，并尽量给出 jd_keywords 与 resume_materials。
5. 不要编造简历中完全不存在的经历；hidden_expectations 写 JD 未明说但常见的隐含要求。"""

    user = f"""【粗评参考】（已由轻量模型批量打分，请在此基础上深化或修正）
{hint_str}

【简历 profile】
{profile_str}

任务：对该岗位与候选人做深度匹配分析，按工具流程执行并以 submit_match_result 结束。"""

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    total_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    final_submit: Optional[Dict[str, Any]] = None
    last_valid_verify_draft: Optional[Dict[str, Any]] = None
    job_id_str = str((job or {}).get("job_id") or "?")

    logger.info(
        "[深度匹配] job_id=%s 开始 | 模型=%s | 最多 %d 轮 | 轮次间隔=%.2fs | tool 压缩保留最近 %d 条全文",
        job_id_str,
        model_name,
        max_rounds,
        _MATCH_AGENT_ROUND_DELAY_SEC,
        max(1, _MATCH_AGENT_TOOL_COMPRESS_KEEP_LAST),
    )

    rounds_completed = 0
    for _round in range(max_rounds):
        round_no = _round + 1
        # 第 2 轮起：上一轮已执行完工具，暂停后再请求 LLM，降低请求过于密集导致的断流
        if _round > 0 and _MATCH_AGENT_ROUND_DELAY_SEC > 0:
            logger.info(
                "[深度匹配] job_id=%s 第 %d 轮前等待 %.2fs（减轻 API 压力）",
                job_id_str,
                round_no,
                _MATCH_AGENT_ROUND_DELAY_SEC,
            )
            time.sleep(_MATCH_AGENT_ROUND_DELAY_SEC)

        messages_api = _messages_for_api_request(messages)
        try:
            resp = _chat_completion_with_retry(
                client,
                model=model_name,
                messages=messages_api,
                tools=MATCH_AGENT_TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )
        except Exception as e:
            if last_valid_verify_draft:
                final_submit = _apply_fallback_from_verify_draft(
                    last_valid_verify_draft,
                    job_id_str,
                    "api_error_after_verify",
                    str(e),
                )
                rounds_completed = round_no
                logger.info(
                    "[深度匹配] job_id=%s 第 %d 轮 LLM 请求失败，已用 verify 缓存兜底结束（本轮失败不计入 token）",
                    job_id_str,
                    round_no,
                )
                break
            raise
        round_pt = round_ct = round_tt = 0
        if hasattr(resp, "usage") and resp.usage:
            round_pt = getattr(resp.usage, "prompt_tokens", 0) or 0
            round_ct = getattr(resp.usage, "completion_tokens", 0) or 0
            round_tt = getattr(resp.usage, "total_tokens", 0) or 0
            total_tokens["prompt_tokens"] += round_pt
            total_tokens["completion_tokens"] += round_ct
            total_tokens["total_tokens"] += round_tt

        msg = resp.choices[0].message
        messages.append(_assistant_message_to_dict(msg))

        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            logger.warning(
                "[深度匹配] job_id=%s 第 %d/%d 轮：模型未请求任何工具（无 tool_calls）| 本回合 token in/out/total=%s/%s/%s",
                job_id_str,
                round_no,
                max_rounds,
                round_pt,
                round_ct,
                round_tt,
            )
            if final_submit:
                rounds_completed = round_no
                logger.info(
                    "[深度匹配] job_id=%s 已有 submit 结果，结束循环 | 实际完成 %d 轮对话",
                    job_id_str,
                    rounds_completed,
                )
                break
            raise ValueError("深度匹配 Agent 未调用工具即结束，且未提交 submit_match_result")

        tool_names = []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            tool_names.append(getattr(fn, "name", "") if fn else "?")
        logger.info(
            "[深度匹配] job_id=%s 第 %d/%d 轮：LLM 已返回 | 本回合 token in/out/total=%s/%s/%s | 本轮工具(%d个): %s",
            job_id_str,
            round_no,
            max_rounds,
            round_pt,
            round_ct,
            round_tt,
            len(tool_calls),
            tool_names,
        )

        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            raw_args = getattr(fn, "arguments", "") if fn else "{}"
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}

            if name == "submit_match_result":
                final_submit = _normalize_submit_payload(args)
                tool_out = json.dumps({"ok": True, "message": "已接收，任务结束"}, ensure_ascii=False)
                logger.info(
                    "[深度匹配] job_id=%s 第 %d/%d 轮：工具「%s」→ 已拿到结果 | match_score=%s recommendation=%s",
                    job_id_str,
                    round_no,
                    max_rounds,
                    name,
                    final_submit.get("match_score"),
                    final_submit.get("recommendation"),
                )
            else:
                tool_out = _execute_tool(name, args, ctx)
                if name == "verify_score":
                    try:
                        vo = json.loads(tool_out)
                        if vo.get("valid") is True and isinstance(args.get("draft"), dict):
                            last_valid_verify_draft = copy.deepcopy(args["draft"])
                            logger.info(
                                "[深度匹配] job_id=%s 第 %d/%d 轮：verify_score(valid=true)，已缓存 draft 供断流或未 submit 兜底",
                                job_id_str,
                                round_no,
                                max_rounds,
                            )
                    except Exception:
                        pass
                logger.info(
                    "[深度匹配] job_id=%s 第 %d/%d 轮：工具「%s」→ 已执行并返回 | 回复长度=%d 预览=%s",
                    job_id_str,
                    round_no,
                    max_rounds,
                    name,
                    len(tool_out),
                    _preview_for_log(tool_out),
                )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_out,
                }
            )

        rounds_completed = round_no
        if final_submit:
            logger.info(
                "[深度匹配] job_id=%s 第 %d/%d 轮：已收到 submit_match_result，结束 | 共完成 %d 轮对话",
                job_id_str,
                round_no,
                max_rounds,
                rounds_completed,
            )
            break

    if not final_submit:
        if last_valid_verify_draft:
            final_submit = _apply_fallback_from_verify_draft(
                last_valid_verify_draft,
                job_id_str,
                "no_submit_within_max_rounds",
                "",
            )
        else:
            logger.error(
                "[深度匹配] job_id=%s 失败：在 %d 轮内未得到 submit_match_result（已完成轮次=%d），且无 verify 缓存",
                job_id_str,
                max_rounds,
                rounds_completed,
            )
            raise ValueError("深度匹配 Agent 未在限定的轮数内调用 submit_match_result")

    logger.info(
        "[深度匹配] job_id=%s 成功结束 | 总轮次=%d | 累计 token in/out/total=%s/%s/%s",
        job_id_str,
        rounds_completed,
        total_tokens.get("prompt_tokens"),
        total_tokens.get("completion_tokens"),
        total_tokens.get("total_tokens"),
    )
    return final_submit, total_tokens


def run_match_agent(
    profile: Dict[str, Any],
    job: Dict[str, Any],
    model_id: str,
    coarse_hint: Optional[Dict[str, Any]] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    深度匹配入口：MATCH_AGENT_MODE=single（默认）单次 submit + 后置校验重试；
    MATCH_AGENT_MODE=loop 多轮工具循环（与旧行为一致）。
    """
    if _match_agent_mode() == "loop":
        logger.info("[深度匹配] 使用 loop 模式（MATCH_AGENT_MODE=loop）")
        return run_match_agent_loop(
            profile,
            job,
            model_id,
            coarse_hint=coarse_hint,
            preferences=preferences,
        )
    logger.info("[深度匹配] 使用 single 模式（默认）")
    return run_match_agent_single(
        profile,
        job,
        model_id,
        coarse_hint=coarse_hint,
        preferences=preferences,
    )
