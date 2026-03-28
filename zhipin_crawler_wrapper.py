"""
爬虫包装器：封装爬虫调用，支持任务管理和确认机制
"""
import time
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class ZhipinCrawlerWrapper:
    """爬虫包装器，支持任务管理和确认机制"""
    
    def __init__(self, crawler, task_id: Optional[str] = None):
        """
        初始化包装器
        
        Args:
            crawler: ZhipinCrawler 实例
            task_id: 任务ID（用于任务管理）
        """
        self.crawler = crawler
        self.task_id = task_id
    
    def check_login_and_wait(self, search_url: str):
        """
        检查是否需要登录，如果需要则等待用户确认
        
        Args:
            search_url: 搜索URL
        """
        logger.info(f"[包装器] check_login_and_wait 开始，URL: {search_url[:80]}...")
        
        # 更精确的登录页面检测：检查是否包含登录表单或登录按钮
        page_text = self.crawler.page.html
        page_url = self.crawler.page.url
        
        # 检查是否是登录页面（更精确的判断）
        is_login_page = (
            '登录' in page_text or 'login' in page_text.lower()
        ) and (
            '登录' in page_text[:500] or  # 页面开头部分包含"登录"（更可能是登录页面）
            '/login' in page_url.lower() or  # URL包含login
            '登录页' in page_text or
            '请登录' in page_text or
            '立即登录' in page_text
        )
        
        # 如果页面已经包含职位列表相关的关键词，说明已经登录成功
        is_already_logged_in = (
            'job-list' in page_text.lower() or
            '职位列表' in page_text or
            'zpData' in page_text or
            'jobName' in page_text
        )
        
        logger.info(f"[包装器] 页面检测结果: is_login_page={is_login_page}, is_already_logged_in={is_already_logged_in}")
        logger.info(f"[包装器] 当前页面URL: {page_url}")
        
        if not is_login_page or is_already_logged_in:
            # 不需要登录，直接返回
            logger.info("[包装器] 无需登录，直接返回")
            return
        
        logger.warning("⚠️ 检测到登录页面")
        
        if not self.task_id:
            # 如果没有任务ID（命令行模式），使用原来的 input() 方式
            logger.info("💡 提示：请在浏览器中手动登录，登录完成后按 Enter 继续...")
            input("登录完成后按 Enter 继续...")
            self.crawler.page.get(search_url)
            time.sleep(3)
            return
        
        # Web 操作台模式：使用任务管理器等待确认
        from task_manager import task_manager
        
        message = "⚠️ 检测到需要登录，请在浏览器中手动登录，登录完成后点击下方确认按钮继续"
        
        # 等待用户确认（无限等待，直到用户点击确认）
        logger.info(f"[包装器] 开始等待用户确认，任务ID: {self.task_id}")
        logger.info(f"[包装器] 当前线程: {threading.current_thread().name}")
        
        try:
            confirmed = task_manager.wait_for_confirm(
                self.task_id,
                message,
                timeout=None  # 无限等待
            )
            
            logger.info(f"[包装器] wait_for_confirm 返回，结果: {confirmed}, 线程: {threading.current_thread().name}")
        except Exception as e:
            logger.error(f"[包装器] wait_for_confirm 异常: {e}", exc_info=True)
            raise
        
        if not confirmed:
            logger.error("[包装器] 用户未确认登录，任务已取消")
            raise Exception("用户未确认登录，任务已取消")
        
        logger.info("[包装器] 用户已确认登录，重新访问搜索页面...")
        
        try:
            # 重新访问搜索页面（刷新页面，确保登录状态生效）
            logger.info(f"[包装器] 正在访问: {search_url}")
            self.crawler.page.get(search_url)
            logger.info("[包装器] 页面访问完成，等待3秒...")
            time.sleep(3)
            
            logger.info("[包装器] 页面重新访问完成，检查登录状态...")
            
            # 再次检查是否还需要登录（防止登录失败）
            page_text_after = self.crawler.page.html
            page_url_after = self.crawler.page.url
            logger.info(f"[包装器] 重新访问后页面URL: {page_url_after}")
            
            # 更精确的检查：如果页面包含职位列表，说明登录成功
            is_still_login_page = (
                ('登录' in page_text_after[:500] or 'login' in page_text_after.lower()[:500])
                and not ('job-list' in page_text_after.lower() or '职位列表' in page_text_after or 'zpData' in page_text_after)
            )
            
            if is_still_login_page:
                logger.warning("⚠️ 重新访问后仍然检测到登录页面，可能登录失败或需要更多时间")
                logger.info("[包装器] 但继续执行，让爬虫自己处理（用户可能已经登录，只是页面检测延迟）")
                # 不再次等待，直接继续（用户已经点击确认，假设已登录）
            else:
                logger.info("✅ 登录检查通过，可以开始爬取")
            
            # 重要：确认后重新访问了页面，需要重置 _page_already_visited 标志
            # 这样 crawl_jobs 方法会知道页面已经准备好，可以开始爬取
            # 但是不要设置为 False，因为页面确实已经访问过了
            # 保持 True，让 crawl_jobs 知道跳过重复访问，直接使用当前页面
            logger.info("[包装器] 保持 _page_already_visited=True，让 crawl_jobs 使用当前页面")
            
        except Exception as e:
            logger.error(f"[包装器] 重新访问页面异常: {e}", exc_info=True)
            # 即使异常也继续，让爬虫自己处理
            logger.warning("[包装器] 页面访问异常，但继续执行")
        
        logger.info("[包装器] check_login_and_wait 方法执行完成，准备返回")
