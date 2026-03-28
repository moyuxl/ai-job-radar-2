"""
Top-N 深度匹配岗位共性分析：基于已入库的 gap_analysis_json 截断字段 + profile，不调 JD。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI
from dotenv import load_dotenv
import os

from resume_extractor import load_resume_json
from match_analyzer import _get_model_config
from match_agent import HEAVY_THRESHOLD

load_dotenv()
logger = logging.getLogger(__name__)

_MAX_GAPS_PER_JOB = 35
_MAX_DESC_LEN = 500
_MAX_HE_LEN = 2000
_MAX_ATS = 50


def _preview_for_log(s: str, max_len: int = 220) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _compact_gap_analysis(parsed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """只保留 gaps / hidden_expectations / ats_keywords / dimension_scores，并截断。"""
    if not isinstance(parsed, dict):
        return {}
    ds = parsed.get("dimension_scores") or {}
    gaps_raw = parsed.get("gaps")
    if not isinstance(gaps_raw, list):
        gaps_raw = []
    trimmed: List[Dict[str, Any]] = []
    for g in gaps_raw[:_MAX_GAPS_PER_JOB]:
        if not isinstance(g, dict):
            continue
        desc = str(g.get("description") or "")
        if len(desc) > _MAX_DESC_LEN:
            desc = desc[: _MAX_DESC_LEN - 1] + "…"
        jk = g.get("jd_keywords") if isinstance(g.get("jd_keywords"), list) else []
        trimmed.append(
            {
                "type": g.get("type"),
                "severity": g.get("severity"),
                "dimension": g.get("dimension"),
                "description": desc,
                "jd_keywords": [str(x) for x in jk[:25]],
            }
        )
    he = str(parsed.get("hidden_expectations") or "")
    if len(he) > _MAX_HE_LEN:
        he = he[: _MAX_HE_LEN - 1] + "…"
    ats = parsed.get("ats_keywords") if isinstance(parsed.get("ats_keywords"), list) else []
    ats = [str(x) for x in ats[:_MAX_ATS]]
    return {
        "dimension_scores": {
            "skill_match": ds.get("skill_match"),
            "experience_match": ds.get("experience_match"),
            "growth_potential": ds.get("growth_potential"),
            "culture_fit": ds.get("culture_fit"),
        },
        "gaps": trimmed,
        "hidden_expectations": he,
        "ats_keywords": ats,
    }


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def run_commonality_analysis(
    resume_path: str,
    model_id: str = "deepseek_chat",
    top_n: int = 10,
    track_params: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    取深度匹配 TopN → 截断 JSON + profile → LLM 共性分析。
    track_params: 与匹配任务一致时，仅在该赛道/关键词批次的岗位内取 TopN（与 get_jobs_by_track 对齐）；
    None 表示不按赛道过滤（历史行为）。

    Returns: (result_dict, token_info)
    result_dict: report (dict), job_count, job_summaries, error (optional)
    """
    from db import COMMONALITY_MIN_JOBS, get_top_deep_matches_for_commonality

    t0 = time.perf_counter()
    logger.info(
        "[共性分析] 开始 | resume_path=%s | top_n=%s | model_id=%s | track_params=%s",
        resume_path,
        top_n,
        model_id,
        "set" if track_params is not None else "none",
    )

    rows = get_top_deep_matches_for_commonality(
        resume_path, limit=top_n, track_params=track_params
    )
    if not rows:
        logger.warning(
            "[共性分析] 中止：无可用深度匹配行 | resume_path=%s | 耗时=%.2fs | 需 match_agent_v1 + gap_analysis_json",
            resume_path,
            time.perf_counter() - t0,
        )
        return {
            "ok": False,
            "error": "没有符合条件的深度匹配记录（需 agent_version=match_agent_v1、已有 gap_analysis_json）。请先对岗位跑深度匹配。",
            "job_count": 0,
            "report": None,
            "job_summaries": [],
        }, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    if len(rows) < COMMONALITY_MIN_JOBS:
        logger.warning(
            "[共性分析] 中止：深度匹配不足 %s 条（当前 %s）| resume_path=%s | 耗时=%.2fs",
            COMMONALITY_MIN_JOBS,
            len(rows),
            resume_path,
            time.perf_counter() - t0,
        )
        return {
            "ok": False,
            "error": (
                f"深度匹配记录不足 {COMMONALITY_MIN_JOBS} 条（当前 {len(rows)} 条）。"
                f"共性分析需要至少 {COMMONALITY_MIN_JOBS} 条带 gap 的深度匹配；"
                f"若 ≥{HEAVY_THRESHOLD} 分不足，请先对更多岗位跑深度匹配（含低于 {HEAVY_THRESHOLD} 分的深度结果亦可凑数）。"
            ),
            "job_count": len(rows),
            "report": None,
            "job_summaries": [],
        }, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    try:
        data = load_resume_json(resume_path)
    except Exception as e:
        logger.exception(
            "[共性分析] 加载简历失败 | resume_path=%s | 耗时=%.2fs",
            resume_path,
            time.perf_counter() - t0,
        )
        return {
            "ok": False,
            "error": f"无法加载简历: {e}",
            "job_count": 0,
            "report": None,
            "job_summaries": [],
        }, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    profile = data.get("profile") or {}
    profile_str = json.dumps(profile, ensure_ascii=False, indent=2)
    profile_chars = len(profile_str)
    logger.info(
        "[共性分析] 简历已加载 | profile 序列化长度=%d 字符 | 参与共性岗位数=%d（请求 top_n=%d；"
        "优先≥%s 分，不足 %d 条时向下补足）",
        profile_chars,
        len(rows),
        top_n,
        HEAVY_THRESHOLD,
        COMMONALITY_MIN_JOBS,
    )
    if len(rows) < top_n:
        logger.info(
            "[共性分析] 实际条数少于 top_n：%d 条（上限请求 %d）",
            len(rows),
            top_n,
        )

    jobs_payload: List[Dict[str, Any]] = []
    job_summaries: List[Dict[str, Any]] = []
    for r in rows:
        jid = r.get("job_id")
        raw = r.get("gap_analysis_json")
        parsed = None
        json_ok = False
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                json_ok = isinstance(parsed, dict)
            except json.JSONDecodeError as ex:
                logger.warning(
                    "[共性分析] gap_analysis_json 解析失败 job_id=%s | %s",
                    jid,
                    ex,
                )
                parsed = None
        compact = _compact_gap_analysis(parsed if isinstance(parsed, dict) else {})
        ng = len(compact.get("gaps") or [])
        nh = len((compact.get("hidden_expectations") or "") or "")
        na = len(compact.get("ats_keywords") or [])
        logger.info(
            "[共性分析] 切片 job_id=%s | score=%s | gaps条数=%d | hidden_exp字符=%d | ats条数=%d | json解析=%s",
            jid,
            r.get("match_score"),
            ng,
            nh,
            na,
            json_ok,
        )
        jobs_payload.append(
            {
                "job_id": jid,
                "job_name": r.get("job_name") or "",
                "company_name": r.get("company_name") or "",
                "match_score": r.get("match_score"),
                "gap_slice": compact,
            }
        )
        job_summaries.append(
            {
                "job_id": jid,
                "job_name": r.get("job_name") or "",
                "company_name": r.get("company_name") or "",
                "match_score": r.get("match_score"),
            }
        )

    system = """你是职业顾问与简历策略顾问。用户会提供「简历 profile」以及若干条岗位在匹配阶段已提炼的结构化差距数据（不含 JD 原文）。
请只做共性归纳与可执行建议，不要编造简历中不存在的经历。

必须只输出一个 JSON 对象（不要 markdown 围栏、不要前后解释），字段如下：
{
  "common_requirements": "字符串，Markdown 可用：这批岗位共同强调的要求主题（技能/经验/工具/软性素质等）",
  "what_i_lack": "字符串，Markdown：对照简历，我系统性不满足或薄弱之处",
  "priority_gaps": [
    { "topic": "主题", "frequency_note": "如 7/10 条岗位出现", "injury_type": "硬伤 或 可包装", "one_line": "一句话说明" }
  ],
  "resume_optimizations": [
    { "module": "模块名，如 技能 / 项目经验 / 工作经历 / 自我评价", "keywords_to_add": ["关键词1","关键词2"], "detail": "具体改法，可写进简历的一句话方向" }
  ]
}
要求：
- priority_gaps 按「出现频率高、应优先解决」排序，至少 3 条（若数据不足则少于 3 条也可）。
- resume_optimizations 必须 2～3 条，具体到模块与关键词，避免空泛战略句。
- injury_type 只能是「硬伤」或「可包装」。"""

    user = f"""【简历 profile】（仅画像，不含求职偏好）
{profile_str}

【深度匹配岗位数】共 {len(jobs_payload)} 条（已按 match_score 降序，同分按 job_id；均为深度匹配结构化切片）

【各岗位差距切片】（每条仅含 dimension_scores、gaps、hidden_expectations、ats_keywords）
{json.dumps(jobs_payload, ensure_ascii=False, indent=2)}

请输出符合上述 schema 的 JSON。"""

    api_key, base_url, model_name = _get_model_config(model_id)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=180.0)

    sys_chars = len(system)
    user_chars = len(user)
    payload_chars = len(json.dumps(jobs_payload, ensure_ascii=False))
    logger.info(
        "[共性分析] 调用 LLM 前 | base_url=%s | model=%s | system字符=%d | user字符=%d | jobs_payload JSON字符=%d",
        base_url,
        model_name,
        sys_chars,
        user_chars,
        payload_chars,
    )

    t_llm = time.perf_counter()
    resp = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.25,
    )
    llm_sec = time.perf_counter() - t_llm

    token_info = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if hasattr(resp, "usage") and resp.usage:
        token_info["prompt_tokens"] = getattr(resp.usage, "prompt_tokens", 0) or 0
        token_info["completion_tokens"] = getattr(resp.usage, "completion_tokens", 0) or 0
        token_info["total_tokens"] = getattr(resp.usage, "total_tokens", 0) or 0

    logger.info(
        "[共性分析] LLM 返回 | 耗时=%.2fs | prompt_tokens=%s | completion_tokens=%s | total_tokens=%s",
        llm_sec,
        token_info["prompt_tokens"],
        token_info["completion_tokens"],
        token_info["total_tokens"],
    )

    content = (resp.choices[0].message.content or "").strip()
    content_len = len(content)
    finish_reason = None
    try:
        ch0 = resp.choices[0]
        finish_reason = getattr(ch0, "finish_reason", None)
    except Exception:
        pass
    logger.info(
        "[共性分析] 模型原文 | 字符数=%d | finish_reason=%s | 预览=%s",
        content_len,
        finish_reason,
        _preview_for_log(content, 280),
    )

    report = _extract_json_object(content)
    if not report:
        logger.warning(
            "[共性分析] JSON 解析失败，将使用原文落入 common_requirements | 原文预览=%s",
            _preview_for_log(content, 400),
        )
        report = {
            "common_requirements": content,
            "what_i_lack": "",
            "priority_gaps": [],
            "resume_optimizations": [],
            "_parse_error": True,
        }
    else:
        pg = report.get("priority_gaps")
        ro = report.get("resume_optimizations")
        n_pg = len(pg) if isinstance(pg, list) else 0
        n_ro = len(ro) if isinstance(ro, list) else 0
        cr_len = len(str(report.get("common_requirements") or ""))
        wi_len = len(str(report.get("what_i_lack") or ""))
        logger.info(
            "[共性分析] JSON 解析成功 | priority_gaps条数=%d | resume_optimizations条数=%d | common_requirements字符=%d | what_i_lack字符=%d",
            n_pg,
            n_ro,
            cr_len,
            wi_len,
        )

    total_sec = time.perf_counter() - t0
    logger.info(
        "[共性分析] 完成 | resume_path=%s | 岗位数=%d | 总耗时=%.2fs | ok=True",
        resume_path,
        len(jobs_payload),
        total_sec,
    )

    return {
        "ok": True,
        "job_count": len(jobs_payload),
        "report": report,
        "job_summaries": job_summaries,
    }, token_info
