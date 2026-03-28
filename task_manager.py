"""
任务管理系统：管理爬虫任务的执行状态、进度和日志
"""
import uuid
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum
from collections import deque
import logging
import queue

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """任务状态枚举"""
    IDLE = "idle"  # 空闲
    RUNNING = "running"  # 运行中
    WAITING_CONFIRM = "waiting_confirm"  # 等待用户确认（如登录确认）
    COMPLETED = "completed"  # 完成
    FAILED = "failed"  # 失败
    CANCELLED = "cancelled"  # 已取消


class TaskManager:
    """任务管理器（单例模式）"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(TaskManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.tasks: Dict[str, Dict] = {}  # 任务ID -> 任务信息
        self.task_lock = threading.Lock()
        self._initialized = True
    
    def create_task(self, task_type: str, params: Dict) -> str:
        """
        创建新任务
        
        Args:
            task_type: 任务类型（如 "crawl"）
            params: 任务参数
        
        Returns:
            任务ID
        """
        task_id = str(uuid.uuid4())
        
        # 创建确认事件
        confirm_event = threading.Event()
        
        task_info = {
            "task_id": task_id,
            "task_type": task_type,
            "status": TaskStatus.IDLE.value,
            "params": params,
            "progress": {
                "current": 0,
                "total": 0,
                "percentage": 0.0
            },
            "logs": deque(maxlen=100),  # 最多保存100条日志
            "start_time": None,
            "end_time": None,
            "duration": 0.0,
            "result": {
                "success_count": 0,
                "failed_count": 0,
                "output_file": None,
                "error_summary": []
            },
            "error": None,
            "waiting_message": None,  # 等待确认时的提示消息
            "confirm_event": confirm_event  # 确认事件（threading.Event）
        }
        
        with self.task_lock:
            self.tasks[task_id] = task_info
        
        logger.info(f"创建任务: {task_id}, 类型: {task_type}")
        return task_id
    
    def get_task(self, task_id: str) -> Optional[Dict]:
        """获取任务信息"""
        with self.task_lock:
            return self.tasks.get(task_id)
    
    def update_status(self, task_id: str, status: TaskStatus):
        """更新任务状态"""
        with self.task_lock:
            if task_id in self.tasks:
                self.tasks[task_id]["status"] = status.value
                if status == TaskStatus.RUNNING and self.tasks[task_id]["start_time"] is None:
                    self.tasks[task_id]["start_time"] = datetime.now().isoformat()
                elif status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                    self.tasks[task_id]["end_time"] = datetime.now().isoformat()
                    if self.tasks[task_id]["start_time"]:
                        start = datetime.fromisoformat(self.tasks[task_id]["start_time"])
                        end = datetime.now()
                        self.tasks[task_id]["duration"] = (end - start).total_seconds()
    
    def update_progress(self, task_id: str, current: int, total: int):
        """更新任务进度"""
        with self.task_lock:
            if task_id in self.tasks:
                self.tasks[task_id]["progress"]["current"] = current
                self.tasks[task_id]["progress"]["total"] = total
                if total > 0:
                    self.tasks[task_id]["progress"]["percentage"] = round((current / total) * 100, 2)
    
    def add_log(self, task_id: str, message: str, level: str = "INFO"):
        """添加日志"""
        with self.task_lock:
            if task_id in self.tasks:
                log_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "level": level,
                    "message": message
                }
                self.tasks[task_id]["logs"].append(log_entry)
                # 同时输出到标准日志
                if level == "ERROR":
                    logger.error(f"[任务 {task_id}] {message}")
                elif level == "WARNING":
                    logger.warning(f"[任务 {task_id}] {message}")
                else:
                    logger.info(f"[任务 {task_id}] {message}")
    
    def update_result(self, task_id: str, **kwargs):
        """更新任务结果"""
        with self.task_lock:
            if task_id in self.tasks:
                self.tasks[task_id]["result"].update(kwargs)
    
    def set_error(self, task_id: str, error: str):
        """设置任务错误"""
        with self.task_lock:
            if task_id in self.tasks:
                self.tasks[task_id]["error"] = error
                self.tasks[task_id]["status"] = TaskStatus.FAILED.value
    
    def wait_for_confirm(self, task_id: str, message: str, timeout: Optional[float] = None) -> bool:
        """
        等待用户确认
        
        Args:
            task_id: 任务ID
            message: 等待确认的提示消息
            timeout: 超时时间（秒），None 表示无限等待
        
        Returns:
            是否确认（True）或超时（False）
        """
        with self.task_lock:
            if task_id not in self.tasks:
                logger.error(f"任务 {task_id} 不存在")
                return False
            
            self.tasks[task_id]["status"] = TaskStatus.WAITING_CONFIRM.value
            self.tasks[task_id]["waiting_message"] = message
            confirm_event = self.tasks[task_id]["confirm_event"]
            
            # 确保事件是未设置状态（如果之前被设置过，需要重置）
            confirm_event.clear()
        
        # 添加日志
        self.add_log(task_id, message, "WARNING")
        logger.info(f"任务 {task_id} 等待用户确认...")
        
        # 等待确认（这里会阻塞，直到 confirm_event.set() 被调用）
        logger.info(f"任务 {task_id} 开始等待事件（阻塞中）...")
        if timeout:
            confirmed = confirm_event.wait(timeout=timeout)
        else:
            confirmed = confirm_event.wait()  # 无限等待，直到事件被设置
        
        logger.info(f"任务 {task_id} 事件等待结束，确认结果: {confirmed}")
        
        # 重置状态
        with self.task_lock:
            if task_id in self.tasks:
                self.tasks[task_id]["waiting_message"] = None
                if confirmed:
                    self.tasks[task_id]["status"] = TaskStatus.RUNNING.value
                    self.add_log(task_id, "用户已确认，继续执行", "INFO")
                    logger.info(f"任务 {task_id} 状态已更新为 RUNNING")
                else:
                    self.tasks[task_id]["status"] = TaskStatus.CANCELLED.value
                    self.add_log(task_id, "等待确认超时，任务已取消", "WARNING")
        
        logger.info(f"wait_for_confirm 方法返回: {confirmed}")
        return confirmed
    
    def confirm_task(self, task_id: str) -> bool:
        """
        确认任务继续执行
        
        Args:
            task_id: 任务ID
        
        Returns:
            是否成功确认
        """
        logger.info(f"confirm_task 被调用，任务ID: {task_id}")
        
        with self.task_lock:
            if task_id not in self.tasks:
                logger.warning(f"确认失败：任务 {task_id} 不存在")
                return False
            
            current_status = self.tasks[task_id]["status"]
            logger.info(f"任务 {task_id} 当前状态: {current_status}")
            
            if current_status != TaskStatus.WAITING_CONFIRM.value:
                logger.warning(f"确认失败：任务 {task_id} 状态为 {current_status}，不是等待确认状态")
                return False
            
            confirm_event = self.tasks[task_id]["confirm_event"]
            logger.info(f"任务 {task_id} 准备设置确认事件，事件当前状态: {confirm_event.is_set()}")
            confirm_event.set()
            logger.info(f"任务 {task_id} 确认事件已设置，事件状态: {confirm_event.is_set()}")
            return True
    
    def get_recent_logs(self, task_id: str, limit: int = 20) -> List[Dict]:
        """获取最近的日志"""
        with self.task_lock:
            if task_id in self.tasks:
                logs = list(self.tasks[task_id]["logs"])
                return logs[-limit:] if len(logs) > limit else logs
        return []
    
    def cleanup_old_tasks(self, max_age_hours: int = 24):
        """清理旧任务（超过指定小时数的任务）"""
        with self.task_lock:
            now = datetime.now()
            to_remove = []
            for task_id, task_info in self.tasks.items():
                if task_info["end_time"]:
                    end_time = datetime.fromisoformat(task_info["end_time"])
                    age_hours = (now - end_time).total_seconds() / 3600
                    if age_hours > max_age_hours:
                        to_remove.append(task_id)
            
            for task_id in to_remove:
                del self.tasks[task_id]
                logger.info(f"清理旧任务: {task_id}")


# 全局任务管理器实例
task_manager = TaskManager()
