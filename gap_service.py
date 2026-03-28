"""
差距分析服务：后台任务封装。

仅运行「改写 Agent」（原 Agent 2）。差距数据一律来自「简历匹配」已写入 match_results 的 JSON：
- 深度匹配：gap_analysis_json 列为 submit_match_result 的 JSON 序列化；
- 仅粗评：无 gap_analysis_json 时，用 gaps 等字段合成同结构的改写上下文。
"""
import logging
from typing import Dict

from task_manager import task_manager, TaskStatus
from db import get_job_by_id, get_match_result_row, save_agent_analysis
from resume_extractor import load_resume_json
from gap_agent import (
    run_agent2,
    build_rewrite_context_from_match_row,
    run_agent2_master_from_commonality,
    COMMONALITY_MASTER_JOB_ID,
)

logger = logging.getLogger(__name__)

STAGE_NAMES = {
    "init": "准备中",
    "load_context": "加载匹配差距数据",
    "agent2": "改写建议与评估",
    "load_commonality": "加载共性报告",
    "agent2_master": "主简历改写（共性驱动）",
    "done": "完成",
}


def _gap_items_from_commonality_report(report: Dict) -> list:
    """共性报告 priority_gaps → gap_items 展示结构"""
    items = []
    for g in report.get("priority_gaps") or []:
        if not isinstance(g, dict):
            continue
        inj = str(g.get("injury_type") or "可包装").strip()
        if inj not in ("硬伤", "可包装"):
            inj = "可包装"
        topic = str(g.get("topic") or "").strip()
        freq = str(g.get("frequency_note") or "").strip()
        line = str(g.get("one_line") or "").strip()
        desc = topic
        if freq:
            desc += f"（{freq}）"
        if line:
            desc += " — " + line
        items.append({"type": inj, "dimension": "", "description": desc})
    return items


def _materials_from_commonality_report(report: Dict) -> list:
    """resume_optimizations → materials 列表展示"""
    out = []
    for o in report.get("resume_optimizations") or []:
        if not isinstance(o, dict):
            continue
        kws = o.get("keywords_to_add") or []
        if not isinstance(kws, list):
            kws = []
        out.append(
            {
                "module": o.get("module") or "",
                "keywords": [str(x) for x in kws[:30]],
                "detail": o.get("detail") or "",
            }
        )
    return out


def _log_tool_call(tool_name: str, result_summary: str, token_info: Dict):
    pt = token_info.get("prompt_tokens", 0) or 0
    ct = token_info.get("completion_tokens", 0) or 0
    tt = token_info.get("total_tokens", 0) or 0
    logger.info(
        f"[工具调用] {tool_name} | 返回: {result_summary} | Token 输入 {pt}, 输出 {ct}, 合计 {tt}"
    )


def run_gap_task(task_id: str, job_id: str, resume_path: str, model_id: str = "deepseek_chat"):
    """后台执行差距分析：从 match_results 读 JSON → 仅调用改写 Agent（submit_rewrite_result）。"""
    try:
        logger.info(f"[任务 {task_id}] ========== 差距分析任务开始 ==========")
        task_manager.update_status(task_id, TaskStatus.RUNNING)
        task_manager.add_log(task_id, "开始差距分析（仅改写 Agent）", "INFO")
        task_manager.update_result(task_id, current_stage=STAGE_NAMES["init"])

        logger.info(f"[任务 {task_id}] 【准备阶段】加载简历: {resume_path}")
        data = load_resume_json(resume_path)
        profile = data.get("profile", {})
        logger.info(f"[任务 {task_id}] 【准备阶段】加载岗位: {job_id}")
        job = get_job_by_id(job_id)
        if not job:
            task_manager.add_log(task_id, f"岗位 {job_id} 不存在", "ERROR")
            task_manager.set_error(task_id, "岗位不存在")
            logger.error(f"[任务 {task_id}] 岗位不存在，任务终止")
            return
        if not job.get("job_desc"):
            task_manager.add_log(task_id, "岗位无职位描述", "WARNING")
            logger.warning(f"[任务 {task_id}] 岗位无职位描述")

        mr = get_match_result_row(job_id, resume_path)
        if not mr:
            msg = "请先在「匹配」中对该岗位完成评分，再运行差距分析（需读取 match_results 中的差距 JSON）"
            task_manager.add_log(task_id, msg, "ERROR")
            task_manager.set_error(task_id, msg)
            logger.error(f"[任务 {task_id}] 无 match_results 记录")
            return

        task_manager.update_result(task_id, current_stage=STAGE_NAMES["load_context"])
        task_manager.add_log(task_id, f"阶段: {STAGE_NAMES['load_context']}", "INFO")
        gap_context = build_rewrite_context_from_match_row(mr, profile, job)
        _gaj = mr.get("gap_analysis_json")
        if isinstance(_gaj, str):
            has_deep = bool(_gaj.strip())
        else:
            has_deep = bool(_gaj)
        task_manager.add_log(
            task_id,
            f"已加载差距上下文 | 深度JSON={'是' if has_deep else '否（粗评 gaps 合成）'} | gap_items={len(gap_context.get('gap_items', []))}",
            "INFO",
        )
        logger.info(
            f"[任务 {task_id}] 【差距来源】match_results | 深度={has_deep} | materials={len(gap_context.get('materials', []))}"
        )

        logger.info(f"[任务 {task_id}] ---------- {STAGE_NAMES['agent2']} ----------")
        task_manager.update_result(task_id, current_stage=STAGE_NAMES["agent2"])
        task_manager.add_log(task_id, f"阶段: {STAGE_NAMES['agent2']}", "INFO")
        logger.info(f"[任务 {task_id}] 【改写 Agent】submit_rewrite_result")
        agent2_result, t2 = run_agent2(profile, job, gap_context, model_id)

        years_verdict = gap_context.get("years_verdict", {})
        is_years_hard = years_verdict.get("is_hard_injury", False)
        rewrites = agent2_result.get("rewrites", []) if not is_years_hard else []
        _log_tool_call(
            "submit_rewrite_result",
            f"rewrites={len(rewrites)}, eval_before={agent2_result.get('eval_before')}, eval_after={agent2_result.get('eval_after')}",
            t2,
        )
        task_manager.add_log(
            task_id,
            f"改写 Agent 完成 | Token 输入 {t2.get('prompt_tokens', 0)}, 输出 {t2.get('completion_tokens', 0)}, 合计 {t2.get('total_tokens', 0)}",
            "INFO",
        )

        eval_after = agent2_result.get("eval_after") or {}
        scores = [
            eval_after.get("skill_match"),
            eval_after.get("experience_match"),
            eval_after.get("growth_potential"),
            eval_after.get("culture_fit"),
        ]
        valid = [x for x in scores if x is not None and x != ""]
        total_after = round(sum(valid) / len(valid)) if valid else 0

        if total_after < 70:
            recommendation = "不建议投递"
            recommendation_msg = "该岗位不建议投递，核心差距不可通过简历改写弥补"
        elif total_after < 75:
            recommendation = "可以试但概率低"
            recommendation_msg = ""
        elif total_after < 80:
            recommendation = "谨慎投递"
            recommendation_msg = ""
        else:
            recommendation = "投递"
            recommendation_msg = ""

        result = {
            "years_verdict": years_verdict,
            "gap_items": gap_context.get("gap_items", []),
            "materials": gap_context.get("materials", []),
            "rewrites": rewrites,
            "eval_before": agent2_result.get("eval_before"),
            "eval_after": agent2_result.get("eval_after"),
            "is_years_hard_injury": is_years_hard,
            "recommendation": recommendation,
            "recommendation_msg": recommendation_msg,
            "token_info": {
                "prompt_tokens": t2.get("prompt_tokens", 0),
                "completion_tokens": t2.get("completion_tokens", 0),
                "total_tokens": t2.get("total_tokens", 0),
            },
        }

        save_agent_analysis(
            job_id=job_id,
            resume_path=resume_path,
            years_verdict=result.get("years_verdict", {}),
            gap_items=result.get("gap_items", []),
            materials=result.get("materials", []),
            rewrites=result.get("rewrites"),
            eval_before=result.get("eval_before"),
            eval_after=result.get("eval_after"),
            is_years_hard_injury=result.get("is_years_hard_injury", False),
            recommendation=result.get("recommendation", ""),
            recommendation_msg=result.get("recommendation_msg", ""),
        )
        logger.info(f"[任务 {task_id}] 【后处理】已保存至数据库 | 推荐={recommendation}")

        task_manager.update_result(task_id, current_stage=STAGE_NAMES["done"])
        task_manager.add_log(task_id, "差距分析完成", "INFO")
        logger.info(f"[任务 {task_id}] ========== 差距分析任务完成 | 总 Token {t2.get('total_tokens', 0)} ==========")
        # 必须先写入 result（含 resume_path），再标记 COMPLETED，否则轮询可能在 resume_path 未写入时读到已完成
        task_manager.update_result(
            task_id,
            job_id=job_id,
            resume_path=resume_path,
            result=result,
        )
        task_manager.update_status(task_id, TaskStatus.COMPLETED)
    except Exception as e:
        logger.exception("差距分析失败")
        task_manager.add_log(task_id, str(e), "ERROR")
        task_manager.set_error(task_id, str(e))


def run_master_rewrite_task(
    task_id: str,
    resume_path: str,
    model_id: str,
    commonality_report: Dict,
):
    """基于共性报告的一次主简历改写（不落单岗 JD）。"""
    try:
        logger.info(f"[任务 {task_id}] ========== 主简历改写（共性）开始 ==========")
        task_manager.update_status(task_id, TaskStatus.RUNNING)
        task_manager.add_log(task_id, "开始主简历改写（共性驱动）", "INFO")
        task_manager.update_result(task_id, current_stage=STAGE_NAMES["init"])

        if not isinstance(commonality_report, dict) or not commonality_report:
            task_manager.set_error(task_id, "共性报告为空或格式错误")
            task_manager.add_log(task_id, "共性报告无效", "ERROR")
            return

        logger.info(f"[任务 {task_id}] 【准备】加载简历: {resume_path}")
        data = load_resume_json(resume_path)
        profile = data.get("profile") or {}

        task_manager.update_result(task_id, current_stage=STAGE_NAMES["load_commonality"])
        task_manager.add_log(task_id, f"阶段: {STAGE_NAMES['load_commonality']}", "INFO")

        task_manager.update_result(task_id, current_stage=STAGE_NAMES["agent2_master"])
        task_manager.add_log(task_id, f"阶段: {STAGE_NAMES['agent2_master']}", "INFO")
        agent2_result, t2 = run_agent2_master_from_commonality(profile, commonality_report, model_id)

        _log_tool_call(
            "submit_rewrite_result(主简历)",
            f"rewrites={len(agent2_result.get('rewrites', []))}, eval_after={agent2_result.get('eval_after')}",
            t2,
        )
        task_manager.add_log(
            task_id,
            f"主简历改写完成 | Token 输入 {t2.get('prompt_tokens', 0)}, 输出 {t2.get('completion_tokens', 0)}, 合计 {t2.get('total_tokens', 0)}",
            "INFO",
        )

        gap_items = _gap_items_from_commonality_report(commonality_report)
        materials = _materials_from_commonality_report(commonality_report)
        years_verdict = {
            "jd_min_years": None,
            "jd_max_years": None,
            "resume_years": None,
            "is_hard_injury": False,
        }

        eval_after = agent2_result.get("eval_after") or {}
        scores = [
            eval_after.get("skill_match"),
            eval_after.get("experience_match"),
            eval_after.get("growth_potential"),
            eval_after.get("culture_fit"),
        ]
        valid = [x for x in scores if x is not None and x != ""]
        total_after = round(sum(valid) / len(valid)) if valid else 0

        if total_after < 70:
            recommendation = "不建议投递"
            recommendation_msg = "改后预期仍偏低，建议继续补强经历或目标岗位"
        elif total_after < 75:
            recommendation = "可以试但概率低"
            recommendation_msg = ""
        elif total_after < 80:
            recommendation = "谨慎投递"
            recommendation_msg = ""
        else:
            recommendation = "投递"
            recommendation_msg = ""

        result = {
            "mode": "master_commonality",
            "years_verdict": years_verdict,
            "gap_items": gap_items,
            "materials": materials,
            "rewrites": agent2_result.get("rewrites", []),
            "eval_before": agent2_result.get("eval_before"),
            "eval_after": agent2_result.get("eval_after"),
            "is_years_hard_injury": False,
            "recommendation": recommendation,
            "recommendation_msg": recommendation_msg,
            "token_info": {
                "prompt_tokens": t2.get("prompt_tokens", 0),
                "completion_tokens": t2.get("completion_tokens", 0),
                "total_tokens": t2.get("total_tokens", 0),
            },
        }

        save_agent_analysis(
            job_id=COMMONALITY_MASTER_JOB_ID,
            resume_path=resume_path,
            years_verdict=years_verdict,
            gap_items=gap_items,
            materials=materials,
            rewrites=result.get("rewrites"),
            eval_before=result.get("eval_before"),
            eval_after=result.get("eval_after"),
            is_years_hard_injury=False,
            recommendation=result.get("recommendation", ""),
            recommendation_msg=result.get("recommendation_msg", ""),
        )
        logger.info(f"[任务 {task_id}] 已保存 agent_analysis | job_id={COMMONALITY_MASTER_JOB_ID}")

        task_manager.update_result(task_id, current_stage=STAGE_NAMES["done"])
        task_manager.add_log(task_id, "主简历改写任务完成", "INFO")
        # 必须先写入 result（含 resume_path），再标记 COMPLETED，否则轮询可能在 resume_path 未写入时读到已完成
        task_manager.update_result(
            task_id,
            job_id=COMMONALITY_MASTER_JOB_ID,
            resume_path=resume_path,
            result=result,
        )
        task_manager.update_status(task_id, TaskStatus.COMPLETED)
    except Exception as e:
        logger.exception("主简历改写失败")
        task_manager.add_log(task_id, str(e), "ERROR")
        task_manager.set_error(task_id, str(e))
