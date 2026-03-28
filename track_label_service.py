"""
赛道标注服务：封装赛道标注任务，支持 Web 单独触发、后台执行与状态更新。
"""
import threading
import logging
from typing import Optional

from task_manager import task_manager
from track_labeler import run_track_label_task

logger = logging.getLogger(__name__)


def start_track_label_task(
    model_id: str = "",
    only_unlabeled: bool = True,
    limit: int = 99999,
) -> str:
    """
    启动赛道标注任务（异步）。默认处理 DB 中全部符合条件的岗位。

    Args:
        model_id: 模型 ID（deepseek_chat/supermind/...），空时使用默认（DeepSeek Chat）
        only_unlabeled: 是否只标注尚未标注的岗位
        limit: 最多标注条数（默认 99999，即全部）

    Returns:
        任务 ID
    """
    task_id = task_manager.create_task("track_label", {
        "model_id": model_id,
        "only_unlabeled": only_unlabeled,
        "limit": limit,
    })

    thread = threading.Thread(
        target=run_track_label_task,
        args=(task_id, model_id or None, only_unlabeled, limit),
        daemon=True,
    )
    thread.start()
    return task_id
