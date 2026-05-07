"""
启动「排除过期岗位」后台任务（与赛道标注类似的异步线程）。
"""
import threading
import logging

from task_manager import task_manager
from job_expiry_check import run_job_expiry_check_task

logger = logging.getLogger(__name__)


def start_job_expiry_check_task(
    delay_sec: float = 1.5,
    limit: int = 500,
    open_cooldown_days: int = 7,
) -> str:
    """
    创建任务并返回 task_id。
    delay_sec: 每条详情页间隔（秒），避免触发风控。
    limit: 本次最多检测条数（按未关闭优先、最近抓取优先）。
    open_cooldown_days: 在招(open)岗位距上次检测未满该天数则跳过（0=不冷却）。
    """
    task_id = task_manager.create_task(
        "job_expiry",
        {
            "delay_sec": delay_sec,
            "limit": limit,
            "open_cooldown_days": open_cooldown_days,
        },
    )
    thread = threading.Thread(
        target=run_job_expiry_check_task,
        args=(task_id, delay_sec, limit, open_cooldown_days),
        daemon=True,
    )
    thread.start()
    return task_id
