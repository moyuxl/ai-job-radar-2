"""
任务用时估算：基于实测单位用时，计算预计完成时间

单位用时来源（持续按新样本微调）：
- 岗位列表：终端 2026-03-13，2 页 30 条 ≈ 7 秒 → 约 3.5 秒/页
- 语义分析：30 条 ≈ 28 秒 → 约 0.93 秒/条
- 详情爬取：原样本约 4.2 秒/条；结合 2026-03-23 实测（75 条成功、总用时约 8 分 47 秒，
  含列表、可选过滤与详情等综合阶段，约 7 秒/条量级）将详情阶段上调校准
- 深度分析：终端 2026-03-13 11:14–11:20，15 条 ≈ 343 秒 → 约 22.9 秒/条（间隔 + LLM）
- 赛道标注：2026-03-23 实测约 75 条共 2 分钟 → 约 1.6 秒/条（批量 LLM 标注）
- 岗位匹配：2026-03-23 实测约 69 条共 9 分钟（540 秒，含硬筛后粗评批次 + 深度 Match Agent 等）→ 均摊 540/69 秒/条

界面展示：format_estimated_range 返回「HH:MM 开始，预计 HH:MM 完成（估算）」，
实际以日志中的「总用时」为准。
"""
from datetime import datetime, timedelta
from typing import Dict, Optional


# 单位用时（秒）
SEC_PER_PAGE = 3.5  # 岗位列表每页
JOBS_PER_PAGE = 15  # 每页约 15 条岗位
SEC_PER_JOB_FILTER = 1.0  # 语义过滤每条
SEC_PER_JOB_DETAIL = 5.2  # 详情爬取每条（按 2026-03-23 长任务样本上调，与旧 4.2 之间折中）
SEC_PER_JOB_ANALYSIS = 23.0  # 深度分析每条（2 秒间隔 + ~21 秒 LLM 调用）
# 赛道标注：实测约 75 条共 2 分钟（120 秒）→ 均摊 120/75 秒/条
SEC_PER_JOB_TRACK_LABEL = 120.0 / 75
# 匹配评分：实测约 69 条共 9 分钟（540 秒，粗评+深度等整段任务）
SEC_PER_JOB_MATCH = 540.0 / 69

# 固定开销（秒）
INIT_OVERHEAD = 10  # 页面加载、登录检查等（略上调）
ANALYSIS_INIT = 5  # 读取 Excel 等


def estimate_crawl_duration(
    max_pages: int,
    crawl_details: bool,
    enable_llm_filter: bool,
    job_count: Optional[int] = None,
) -> float:
    """
    估算爬取任务总用时（秒）

    Args:
        max_pages: 最大页数
        crawl_details: 是否爬取详情
        enable_llm_filter: 是否启用语义过滤
        job_count: 已知岗位数（列表爬完后有值），None 时用 max_pages * JOBS_PER_PAGE 估算

    Returns:
        预计总秒数
    """
    jobs = job_count if job_count is not None else max_pages * JOBS_PER_PAGE

    # 1. 岗位列表
    list_sec = max_pages * SEC_PER_PAGE

    # 2. 语义过滤
    filter_sec = jobs * SEC_PER_JOB_FILTER if enable_llm_filter else 0

    # 3. 详情爬取
    detail_sec = jobs * SEC_PER_JOB_DETAIL if crawl_details else 0

    return INIT_OVERHEAD + list_sec + filter_sec + detail_sec


def estimate_analysis_duration(job_count: int) -> float:
    """
    估算分析任务总用时（秒）

    Args:
        job_count: 岗位数量

    Returns:
        预计总秒数
    """
    return ANALYSIS_INIT + job_count * SEC_PER_JOB_ANALYSIS


def estimate_track_label_duration(job_count: int) -> float:
    """
    估算赛道标注总用时（秒）。

    基于实测：约 75 条、总用时约 2 分钟（读库与批处理已摊入每条约 1.6 秒）。
    """
    return job_count * SEC_PER_JOB_TRACK_LABEL


def estimate_match_duration(job_count: int) -> float:
    """
    估算匹配评分任务总用时（秒）。

    基于实测：约 69 条、总用时约 9 分钟（硬筛、赛道筛选、粗评批次、深度匹配等整段）。
    """
    return job_count * SEC_PER_JOB_MATCH


def get_estimated_end_time(
    start_time_iso: Optional[str],
    task_type: str,
    params: Dict,
    progress: Dict,
    result: Dict,
) -> Optional[str]:
    """
    计算预计完成时间，返回格式如 "10:30"

    Args:
        start_time_iso: 任务开始时间 ISO 字符串
        task_type: "crawl" / "analysis" / "track_label" / "match"
        params: 任务参数
        progress: 进度 {current, total, percentage}
        result: 结果 {job_count?, success_count?, ...}

    Returns:
        预计完成时间字符串 "HH:MM"，无法计算时返回 None
    """
    if not start_time_iso or task_type not in ("crawl", "analysis", "track_label", "match"):
        return None

    try:
        start = datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
        if start.tzinfo:
            start = start.replace(tzinfo=None)
    except Exception:
        return None

    if task_type == "crawl":
        max_pages = params.get("max_pages", 1)
        crawl_details = params.get("crawl_details", True)
        enable_llm_filter = params.get("enable_llm_filter", False)
        job_count = result.get("job_count")
        total_sec = estimate_crawl_duration(
            max_pages, crawl_details, enable_llm_filter, job_count
        )
    elif task_type == "track_label":
        job_count = progress.get("total") or 0
        if job_count <= 0:
            return None
        total_sec = estimate_track_label_duration(job_count)
    elif task_type == "match":
        job_count = progress.get("total") or 0
        if job_count <= 0:
            return None
        total_sec = estimate_match_duration(job_count)
    else:
        job_count = progress.get("total") or result.get("success_count", 0)
        if job_count <= 0:
            return None
        total_sec = estimate_analysis_duration(job_count)

    end_time = start + timedelta(seconds=total_sec)
    return end_time.strftime("%H:%M")


def format_estimated_range(start_time_iso: Optional[str], end_time_str: Optional[str]) -> str:
    """
    格式化预计时间范围，例如：
    "09:18 开始，预计 09:26 完成（估算）"
    完成时刻为估算值；真实耗时见任务日志末尾「总用时」。
    """
    if not end_time_str:
        return ""
    try:
        if start_time_iso:
            start = datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
            if start.tzinfo:
                start = start.replace(tzinfo=None)
            start_str = start.strftime("%H:%M")
            return f"{start_str} 开始，预计 {end_time_str} 完成（估算）"
        return f"预计 {end_time_str} 完成（估算）"
    except Exception:
        return f"预计 {end_time_str} 完成（估算）"
