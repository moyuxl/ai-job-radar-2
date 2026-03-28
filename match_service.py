"""
匹配服务：后台任务封装，硬筛 + LLM 批量评分 + 持久化
"""
import logging
import os
from typing import Dict, List, Optional, Tuple

from task_manager import task_manager, TaskStatus
from db import (
    get_jobs_by_track,
    get_job_by_id,
    save_match_result,
    get_matched_job_ids,
    get_match_deep_scan_stats,
    get_match_rows_needing_deep_backfill,
)
from resume_extractor import load_resume_json
from match_analyzer import (
    hard_filter_jobs,
    _call_llm_match_batch,
    MATCH_BATCH_SIZE,
    gaps_to_display_strings,
)
from match_agent import (
    run_match_agent,
    build_advice_from_submit,
    MATCH_AGENT_VERSION,
    COARSE_AGENT_VERSION,
    HEAVY_THRESHOLD,
)

logger = logging.getLogger(__name__)

# 单次「开始匹配」任务中，对「≥阈值且缺深度」补跑深度的最多条数（防 token 爆炸）
_MATCH_DEEP_BACKFILL_MAX = int(os.getenv("MATCH_DEEP_BACKFILL_MAX", "50"))


def _coarse_result_dict_from_stored_match_row(mr: Dict) -> Dict:
    """从 match_results 行构造与粗评 batch 一致的 r，供补跑深度。"""
    coarse = int(mr.get("coarse_score") or mr.get("match_score") or 0)
    ds = {
        "skill_match": int(mr.get("skill_match") or 0),
        "experience_match": int(mr.get("experience_match") or 0),
        "growth_potential": int(mr.get("growth_potential") or 0),
        "culture_fit": int(mr.get("culture_fit") or 0),
    }
    gaps_raw = mr.get("gaps")
    if not isinstance(gaps_raw, list):
        gaps_raw = []
    return {
        "job_id": mr.get("job_id"),
        "match_score": coarse,
        "dimension_scores": ds,
        "strengths": mr.get("strengths") if isinstance(mr.get("strengths"), list) else [],
        "gaps": gaps_raw,
        "advice": mr.get("advice") or "",
    }


def _run_deep_backfill_for_resume(
    resume_path: str,
    profile: Dict,
    prefs: Dict,
    model_id: str,
    task_id: str,
) -> Tuple[Dict[str, int], int]:
    """
    扫描并补跑：match_score >= HEAVY_THRESHOLD 但仍非深度匹配的记录。
    返回 (token 累计, 成功处理条数)。
    """
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    stats = get_match_deep_scan_stats(resume_path, HEAVY_THRESHOLD, MATCH_AGENT_VERSION)
    task_manager.add_log(
        task_id,
        (
            f"深度匹配扫描：粗评分数<{HEAVY_THRESHOLD} 的岗位 {stats['below_threshold']} 条（不跑深度，属正常）；"
            f"≥{HEAVY_THRESHOLD} 且仍缺深度 {stats['need_deep_backfill']} 条"
        ),
        "INFO",
    )
    limit = max(0, _MATCH_DEEP_BACKFILL_MAX)
    rows = get_match_rows_needing_deep_backfill(
        resume_path, HEAVY_THRESHOLD, limit, MATCH_AGENT_VERSION
    )
    if stats["need_deep_backfill"] > len(rows) and limit > 0:
        task_manager.add_log(
            task_id,
            f"缺深度岗位多于单次上限 {limit}，本次仅补跑 {len(rows)} 条（可调环境变量 MATCH_DEEP_BACKFILL_MAX）",
            "INFO",
        )
    if not rows:
        return totals, 0

    task_manager.add_log(task_id, f"开始补跑深度匹配：共 {len(rows)} 条", "INFO")
    ok = 0
    for i, mr in enumerate(rows):
        jid = mr.get("job_id")
        job_row = get_job_by_id(jid)
        if not job_row:
            task_manager.add_log(task_id, f"补跑深度跳过 job={jid}（岗位已从库中删除）", "WARNING")
            continue
        r = _coarse_result_dict_from_stored_match_row(mr)
        task_manager.update_progress(task_id, i + 1, max(len(rows), 1))
        try:
            extra = _apply_one_match_from_coarse_result(
                r,
                job_row,
                profile,
                resume_path,
                model_id,
                task_id,
                preferences=prefs,
            )
            totals["prompt_tokens"] += extra["prompt_tokens"]
            totals["completion_tokens"] += extra["completion_tokens"]
            totals["total_tokens"] += extra["total_tokens"]
            ok += 1
        except Exception:
            logger.exception("补跑深度失败 job=%s", jid)
            task_manager.add_log(task_id, f"补跑深度失败 job={jid}", "ERROR")

    task_manager.add_log(task_id, f"深度补跑完成：处理 {ok}/{len(rows)} 条", "INFO")
    return totals, ok


def _apply_one_match_from_coarse_result(
    r: Dict,
    job_row: Optional[Dict],
    profile: Dict,
    resume_path: str,
    model_id: str,
    task_id: str,
    preferences: Optional[Dict] = None,
) -> Dict[str, int]:
    """
    将一条粗评结果写入 DB，必要时跑深度 Match Agent。
    返回除粗评批次外**额外**产生的 token（仅深度匹配部分；粗评由调用方累计）。
    """
    extra = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    jid = r.get("job_id")
    coarse = int(r.get("match_score", 0) or 0)
    coarse_ds = r.get("dimension_scores") or {}

    if job_row is not None and coarse >= HEAVY_THRESHOLD:
        try:
            heavy, t_h = run_match_agent(
                profile,
                job_row,
                model_id,
                coarse_hint={
                    "match_score": coarse,
                    "dimension_scores": coarse_ds,
                    "gaps": r.get("gaps"),
                },
                preferences=preferences or {},
            )
            pt = int(t_h.get("prompt_tokens", 0) or 0)
            ct = int(t_h.get("completion_tokens", 0) or 0)
            tt = int(t_h.get("total_tokens", 0) or 0)
            extra["prompt_tokens"] += pt
            extra["completion_tokens"] += ct
            extra["total_tokens"] += tt
            task_manager.add_log(
                task_id,
                f"深度匹配 job={jid} Token: 输入 {pt}, 输出 {ct}, 合计 {tt}",
                "INFO",
            )
            gap_strs = gaps_to_display_strings(heavy.get("gaps"))
            strengths = heavy.get("strengths") or []
            advice = build_advice_from_submit(heavy)
            save_match_result(
                job_id=jid,
                resume_path=resume_path,
                match_score=heavy["match_score"],
                dimension_scores=heavy["dimension_scores"],
                strengths=strengths,
                gaps=gap_strs,
                advice=advice,
                gap_analysis_json=heavy,
                agent_version=MATCH_AGENT_VERSION,
                coarse_score=coarse,
            )
        except Exception:
            logger.exception("深度匹配失败，已回退为粗评")
            task_manager.add_log(
                task_id,
                f"深度匹配失败 job={jid}，已保存粗评",
                "WARNING",
            )
            save_match_result(
                job_id=jid,
                resume_path=resume_path,
                match_score=coarse,
                dimension_scores=coarse_ds,
                strengths=r.get("strengths") or [],
                gaps=gaps_to_display_strings(r.get("gaps")),
                advice=r.get("advice") or "",
                gap_analysis_json=None,
                agent_version=COARSE_AGENT_VERSION,
                coarse_score=coarse,
            )
    else:
        save_match_result(
            job_id=jid,
            resume_path=resume_path,
            match_score=coarse,
            dimension_scores=coarse_ds,
            strengths=r.get("strengths") or [],
            gaps=gaps_to_display_strings(r.get("gaps")),
            advice=r.get("advice") or "",
            gap_analysis_json=None,
            agent_version=COARSE_AGENT_VERSION,
            coarse_score=coarse,
        )
    return extra


def run_match_task(
    task_id: str,
    resume_path: str,
    track_params: Dict,
    hard_filter_overrides: Dict,
    model_id: str,
):
    """
    后台执行匹配任务
    track_params: company_types, job_natures, job_direction_primaries, min_confidence, source_keywords
    hard_filter_overrides: target_salary_min, target_salary_max, target_cities, company_type_blacklist（临时覆盖）
    """
    try:
        task_manager.update_status(task_id, TaskStatus.RUNNING)
        task_manager.add_log(task_id, "开始匹配任务", "INFO")

        # 加载简历
        data = load_resume_json(resume_path)
        profile = data.get("profile", {})
        prefs = data.get("preferences", {})

        salary_min = hard_filter_overrides.get("target_salary_min")
        if salary_min is None:
            salary_min = int(prefs.get("target_salary_min", 0) or 0)
        else:
            salary_min = int(salary_min)

        salary_max = hard_filter_overrides.get("target_salary_max")
        if salary_max is None:
            salary_max = int(prefs.get("target_salary_max", 0) or 0)
        else:
            salary_max = int(salary_max)

        cities = hard_filter_overrides.get("target_cities")
        if cities is None:
            cities = prefs.get("target_cities") or []
        if isinstance(cities, str):
            cities = [c.strip() for s in cities.split(",") for s in [s]]

        blacklist = hard_filter_overrides.get("company_type_blacklist")
        if blacklist is None:
            blacklist = prefs.get("company_type_blacklist") or []

        # 先扫描并补跑：≥HEAVY_THRESHOLD 但仍非深度匹配的历史记录（与「新岗位粗评」独立）
        bf_totals, bf_count = _run_deep_backfill_for_resume(
            resume_path, profile, prefs, model_id, task_id
        )

        # 赛道筛选拿岗位
        jobs = get_jobs_by_track(
            company_types=track_params.get("company_types") or None,
            job_natures=track_params.get("job_natures") or None,
            job_direction_primaries=track_params.get("job_direction_primaries") or None,
            min_confidence=track_params.get("min_confidence") or None,
            source_keywords=track_params.get("source_keywords") or None,
        )
        task_manager.add_log(task_id, f"赛道筛选得到 {len(jobs)} 条岗位", "INFO")

        # 硬筛
        filtered, hf_stats = hard_filter_jobs(jobs, salary_min, salary_max, cities, blacklist)
        task_manager.add_log(
            task_id,
            (
                f"硬筛：入参 {hf_stats['input']} 条 → 薪资不符 {hf_stats['drop_salary']} 条，"
                f"城市不符 {hf_stats['drop_city']} 条，公司类型黑名单 {hf_stats['drop_blacklist']} 条，"
                f"通过 {hf_stats['pass']} 条"
            ),
            "INFO",
        )

        # 排除已匹配过的岗位，只对新岗位调用 LLM（节省 token）
        already_matched_ids = get_matched_job_ids(resume_path)
        to_score = [j for j in filtered if j.get("job_id") not in already_matched_ids]
        skipped = len(filtered) - len(to_score)
        if skipped > 0:
            task_manager.add_log(
                task_id,
                f"跳过已匹配 {skipped} 条，待评分 {len(to_score)} 条",
                "INFO",
            )
        task_manager.update_progress(task_id, 0, max(len(to_score), 1))

        if not to_score:
            task_manager.add_log(task_id, "无待评分岗位（已匹配过的已跳过）", "INFO")
            task_manager.update_status(task_id, TaskStatus.COMPLETED)
            task_manager.update_result(
                task_id,
                success_count=0,
                failed_count=0,
                total=len(filtered),
                skipped=skipped,
                total_input_tokens=bf_totals["prompt_tokens"],
                total_output_tokens=bf_totals["completion_tokens"],
                total_tokens=bf_totals["total_tokens"],
                resume_path=resume_path,
                deep_backfill_count=bf_count,
            )
            return

        # 逐批 LLM 评分（仅对新岗位）；Token 累计含上方深度补跑
        total_input_tokens = bf_totals["prompt_tokens"]
        total_output_tokens = bf_totals["completion_tokens"]
        total_tokens = bf_totals["total_tokens"]
        success_count = 0
        failed_count = 0
        for start in range(0, len(to_score), MATCH_BATCH_SIZE):
            batch = to_score[start : start + MATCH_BATCH_SIZE]
            task_manager.add_log(
                task_id,
                f"评分第 {start + 1}-{start + len(batch)}/{len(to_score)} 条...",
                "INFO",
            )
            try:
                results, token_info = _call_llm_match_batch(batch, profile, model_id)
                pt = token_info.get("prompt_tokens", 0) or 0
                ct = token_info.get("completion_tokens", 0) or 0
                tt = token_info.get("total_tokens", 0) or 0
                total_input_tokens += pt
                total_output_tokens += ct
                total_tokens += tt
                task_manager.add_log(
                    task_id,
                    f"本批 Token: 输入 {pt}, 输出 {ct}, 合计 {tt}",
                    "INFO",
                )
                for r in results:
                    jid = r.get("job_id")
                    job_row = next(
                        (j for j in batch if str(j.get("job_id")) == str(jid)),
                        None,
                    )
                    extra = _apply_one_match_from_coarse_result(
                        r,
                        job_row,
                        profile,
                        resume_path,
                        model_id,
                        task_id,
                        preferences=prefs,
                    )
                    total_input_tokens += extra["prompt_tokens"]
                    total_output_tokens += extra["completion_tokens"]
                    total_tokens += extra["total_tokens"]
                    success_count += 1
            except Exception as e:
                logger.exception("匹配评分失败")
                task_manager.add_log(task_id, f"本批失败: {e}", "ERROR")
                failed_count += len(batch)

            task_manager.update_progress(task_id, min(start + len(batch), len(to_score)), len(to_score))

        task_manager.add_log(
            task_id,
            (
                f"匹配完成：新岗粗评成功 {success_count}，失败 {failed_count}；"
                f"深度补跑 {bf_count} 条；Token 输入 {total_input_tokens}, 输出 {total_output_tokens}, 合计 {total_tokens}"
            ),
            "INFO",
        )
        task_manager.update_status(task_id, TaskStatus.COMPLETED)
        task_manager.update_result(
            task_id,
            success_count=success_count,
            failed_count=failed_count,
            total=len(filtered),
            skipped=skipped,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_tokens=total_tokens,
            resume_path=resume_path,
            deep_backfill_count=bf_count,
        )
    except Exception as e:
        logger.exception("匹配任务异常")
        task_manager.add_log(task_id, str(e), "ERROR")
        task_manager.set_error(task_id, str(e))


def run_rerun_match_one(task_id: str, job_id: str, resume_path: str, model_id: str):
    """
    对单条岗位重新跑粗评 + 深度 Match Agent（覆盖原 match_results）。
    深度匹配逻辑在 match_agent.run_match_agent（默认单次，可选 MATCH_AGENT_MODE=loop）。
    """
    try:
        task_manager.update_status(task_id, TaskStatus.RUNNING)
        task_manager.add_log(task_id, f"单岗位重新匹配开始: job_id={job_id}", "INFO")
        task_manager.update_progress(task_id, 0, 1)

        job_row = get_job_by_id(job_id)
        if not job_row:
            task_manager.set_error(task_id, "岗位不存在或已从库中删除")
            return

        data = load_resume_json(resume_path)
        profile = data.get("profile", {})
        prefs = data.get("preferences", {})

        results, token_info = _call_llm_match_batch([job_row], profile, model_id)
        pt = int(token_info.get("prompt_tokens", 0) or 0)
        ct = int(token_info.get("completion_tokens", 0) or 0)
        tt = int(token_info.get("total_tokens", 0) or 0)
        task_manager.add_log(
            task_id,
            f"粗评 Token: 输入 {pt}, 输出 {ct}, 合计 {tt}",
            "INFO",
        )

        if not results:
            task_manager.set_error(task_id, "粗评无返回结果")
            return

        r = results[0]
        extra = _apply_one_match_from_coarse_result(
            r, job_row, profile, resume_path, model_id, task_id, preferences=prefs
        )
        total_pt = pt + extra["prompt_tokens"]
        total_ct = ct + extra["completion_tokens"]
        total_tt = tt + extra["total_tokens"]

        task_manager.add_log(
            task_id,
            f"重新匹配完成：合计 Token 输入 {total_pt}, 输出 {total_ct}, 合计 {total_tt}",
            "INFO",
        )
        task_manager.update_progress(task_id, 1, 1)
        task_manager.update_status(task_id, TaskStatus.COMPLETED)
        task_manager.update_result(
            task_id,
            success_count=1,
            failed_count=0,
            job_id=job_id,
            resume_path=resume_path,
            total_input_tokens=total_pt,
            total_output_tokens=total_ct,
            total_tokens=total_tt,
        )
    except Exception as e:
        logger.exception("单岗位重新匹配失败")
        task_manager.add_log(task_id, str(e), "ERROR")
        task_manager.set_error(task_id, str(e))
