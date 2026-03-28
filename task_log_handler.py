"""
任务日志处理器：将爬虫日志重定向到任务管理器
"""
import logging
from typing import Optional


class TaskLogHandler(logging.Handler):
    """自定义日志处理器，将日志发送到任务管理器"""
    
    def __init__(self, task_id: Optional[str] = None):
        super().__init__()
        self.task_id = task_id
    
    def emit(self, record):
        """发送日志记录到任务管理器"""
        if self.task_id:
            from task_manager import task_manager
            
            # 格式化日志消息
            message = self.format(record)
            
            # 根据日志级别添加到任务管理器
            level = record.levelname
            task_manager.add_log(self.task_id, message, level)
    
    def set_task_id(self, task_id: str):
        """设置任务ID"""
        self.task_id = task_id
