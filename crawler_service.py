"""
爬虫服务：封装爬虫功能，支持后台任务执行和状态更新
"""
import os
import time
import threading
import logging
from pathlib import Path
from typing import Dict, Optional

from zhipin_crawler import ZhipinCrawler
from task_manager import task_manager, TaskStatus
from task_log_handler import TaskLogHandler
from zhipin_crawler_wrapper import ZhipinCrawlerWrapper

try:
    from db import init_db, upsert_job_from_crawler, update_job_detail
    HAS_DB = True
except ImportError:
    HAS_DB = False

logger = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    """将秒数格式化为易读的中文用时（用于日志）。"""
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.1f}秒"
    total = int(round(seconds))
    m = total // 60
    s = total % 60
    if m < 60:
        return f"{m}分{s}秒"
    h = m // 60
    m = m % 60
    return f"{h}小时{m}分{s}秒"


def run_crawl_task(task_id: str, params: Dict, output_dir: str = "output"):
    """
    在后台线程中执行爬虫任务
    
    Args:
        task_id: 任务ID
        params: 爬虫参数
            - keyword: 职位关键词
            - city: 城市代码
            - degree: 学历代码
            - experience: 工作经验代码
            - salary: 薪资代码
            - max_pages: 最大页数
            - crawl_details: 是否爬取详情页
            - enable_llm_filter: 是否启用 LLM 语义过滤
            - filter_model_id: 过滤使用的模型 ID
        output_dir: 输出目录
    """
    try:
        # 更新状态为运行中
        task_manager.update_status(task_id, TaskStatus.RUNNING)
        task_manager.add_log(task_id, "开始爬取任务", "INFO")
        crawl_start = time.perf_counter()
        
        # 创建输出目录
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # 设置日志处理器，将爬虫日志重定向到任务管理器
        task_handler = TaskLogHandler(task_id=task_id)
        task_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(message)s')
        task_handler.setFormatter(formatter)
        
        # 获取爬虫的日志记录器并添加处理器
        crawler_logger = logging.getLogger('zhipin_crawler')
        crawler_logger.addHandler(task_handler)
        crawler_logger.setLevel(logging.INFO)
        
        # 同时为包装器的日志记录器添加处理器
        wrapper_logger = logging.getLogger('zhipin_crawler_wrapper')
        wrapper_logger.addHandler(task_handler)
        wrapper_logger.setLevel(logging.INFO)
        
        # 为爬虫服务的日志记录器也添加处理器（用于调试）
        service_logger = logging.getLogger('crawler_service')
        service_logger.addHandler(task_handler)
        service_logger.setLevel(logging.INFO)
        
        # 创建爬虫实例
        crawler = ZhipinCrawler(headless=False)  # 显示浏览器窗口
        
        # 创建包装器，支持确认机制
        crawler_wrapper = ZhipinCrawlerWrapper(crawler, task_id=task_id)
        
        try:
            # 提取参数
            keyword = params.get("keyword", "")
            city = params.get("city", "100010000")
            degree = params.get("degree", "")
            experience = params.get("experience", "")  # 空=不限（显示全部），101=经验不限
            salary = params.get("salary", "")
            max_pages = params.get("max_pages", 1)
            crawl_details = params.get("crawl_details", True)
            enable_llm_filter = params.get("enable_llm_filter", False)
            filter_model_id = params.get("filter_model_id", "") or None
            
            task_manager.add_log(task_id, f"搜索条件: {keyword}, 城市: {city}, 页数: {max_pages}", "INFO")
            
            # 构建搜索 URL（用于登录检查）
            search_url = f'https://www.zhipin.com/web/geek/job?query={keyword}&city={city}'
            if degree:
                search_url += f'&degree={degree}'
            if experience:
                search_url += f'&experience={experience}'
            if salary:
                search_url += f'&salary={salary}'
            
            # 访问搜索页面并检查登录
            task_manager.add_log(task_id, "正在访问搜索页面...", "INFO")
            crawler.page.get(search_url)
            time.sleep(3)  # 等待页面加载
            
            # 标记页面已访问（用于 crawl_jobs 内部判断）
            crawler._page_already_visited = True
            
            # 检查是否需要登录（使用包装器的方法）
            task_manager.add_log(task_id, "检查是否需要登录...", "INFO")
            try:
                logger.info(f"[爬虫服务] 调用 check_login_and_wait，任务ID: {task_id}")
                logger.info(f"[爬虫服务] 当前线程: {threading.current_thread().name}")
                crawler_wrapper.check_login_and_wait(search_url)
                logger.info(f"[爬虫服务] check_login_and_wait 返回，任务ID: {task_id}")
                logger.info(f"[爬虫服务] 准备调用 crawl_jobs，任务ID: {task_id}")
                task_manager.add_log(task_id, "登录检查完成，开始爬取", "INFO")
            except Exception as e:
                error_msg = f"登录确认失败: {str(e)}"
                logger.error(f"[爬虫服务] 登录确认失败: {e}", exc_info=True)
                task_manager.add_log(task_id, error_msg, "ERROR")
                raise
            
            # 开始爬取列表页
            task_manager.add_log(task_id, f"开始爬取列表页（最多 {max_pages} 页）...", "INFO")
            task_manager.update_progress(task_id, 0, max_pages)
            
            jobs = []
            
            # 调用爬虫（此时已经登录，直接爬取）
            # 注意：由于无法修改原爬虫代码，进度跟踪有限
            # 列表页进度会在每页完成后更新（通过日志监控）
            task_manager.add_log(task_id, "调用爬虫 crawl_jobs 方法...", "INFO")
            logger.info(f"[爬虫服务] 开始调用 crawler.crawl_jobs，任务ID: {task_id}")
            logger.info(f"[爬虫服务] 参数: keyword={keyword}, city={city}, max_pages={max_pages}")
            try:
                jobs = crawler.crawl_jobs(
                    keyword=keyword,
                    city=city,
                    degree=degree,
                    experience=experience,
                    salary=salary,
                    max_pages=max_pages,
                    crawl_details=crawl_details,
                    enable_llm_filter=enable_llm_filter,
                    model_id=filter_model_id,
                    task_id=task_id  # 传递 task_id，用于 Web 模式下的确认机制
                )
                logger.info(f"[爬虫服务] crawler.crawl_jobs 执行完成，获取到 {len(jobs)} 条数据，任务ID: {task_id}")
                task_manager.add_log(task_id, f"爬虫 crawl_jobs 方法执行完成，获取到 {len(jobs)} 条数据", "INFO")
            except Exception as e:
                error_msg = f"爬取失败: {str(e)}"
                logger.error(f"[爬虫服务] crawler.crawl_jobs 执行失败: {e}", exc_info=True)
                task_manager.add_log(task_id, error_msg, "ERROR")
                raise
            
            # 更新进度（列表页完成）
            task_manager.update_progress(task_id, max_pages, max_pages)
            task_manager.add_log(task_id, f"列表页爬取完成，获取到 {len(jobs)} 条岗位", "INFO")
            
            # 写入本地数据库（去重、持久化）
            if jobs and HAS_DB:
                try:
                    init_db()
                    crawl_params = {"keyword": keyword, "city": city, "degree": degree, "experience": experience, "salary": salary}
                    for j in jobs:
                        upsert_job_from_crawler(j, crawl_params)
                    for j in jobs:
                        if j.get("职位描述") and j.get("岗位ID"):
                            update_job_detail(j["岗位ID"], j["职位描述"], j.get("公司介绍"))
                    task_manager.add_log(task_id, f"已同步 {len(jobs)} 条岗位到本地数据库", "INFO")
                except Exception as db_e:
                    logger.warning(f"写入数据库失败（不影响 Excel）: {db_e}")
                    task_manager.add_log(task_id, f"数据库写入失败: {db_e}", "WARNING")
            
            # 如果需要爬取详情页，更新进度
            if crawl_details and jobs:
                total_jobs = len(jobs)
                task_manager.update_progress(task_id, 0, total_jobs)
                task_manager.add_log(task_id, f"开始爬取详情页（共 {total_jobs} 条）...", "INFO")
                
                # 详情页爬取会在爬虫内部完成
                # 由于无法实时跟踪，我们只能等待完成
                # 完成后会更新进度
                task_manager.update_progress(task_id, total_jobs, total_jobs)
            
            # 保存到 Excel
            if jobs:
                task_manager.add_log(task_id, "正在保存数据到 Excel...", "INFO")
                
                # 生成文件名
                timestamp = task_id[:8]  # 使用任务ID前8位作为时间戳
                filename = f"boss_{keyword}_{timestamp}.xlsx"
                filepath = output_path / filename
                
                # 保存文件
                saved_path = crawler.save_to_excel(jobs, filename=str(filepath))
                
                if not saved_path:
                    # 如果保存失败，使用默认路径重试
                    saved_path = crawler.save_to_excel(jobs)
                    if saved_path:
                        filepath = Path(saved_path)
                
                # 更新结果
                success_count = len([j for j in jobs if j.get('职位描述') or not crawl_details])
                failed_count = len(jobs) - success_count
                
                output_file_path = saved_path if saved_path else str(filepath.absolute())
                task_manager.update_result(
                    task_id,
                    success_count=success_count,
                    failed_count=failed_count,
                    output_file=output_file_path,
                    job_count=len(jobs),
                )
                
                task_manager.add_log(task_id, f"数据已保存到: {filepath.absolute()}", "INFO")
                task_manager.add_log(task_id, f"成功: {success_count}, 失败: {failed_count}", "INFO")
            else:
                task_manager.add_log(task_id, "未获取到任何数据", "WARNING")
                task_manager.update_result(task_id, success_count=0, failed_count=0, output_file=None)
            
            # 更新状态为完成
            task_manager.update_status(task_id, TaskStatus.COMPLETED)
            elapsed = time.perf_counter() - crawl_start
            task_manager.add_log(
                task_id,
                f"爬取任务完成，总用时: {_format_duration(elapsed)}",
                "INFO",
            )
            
        except KeyboardInterrupt:
            task_manager.add_log(task_id, "用户中断爬取", "WARNING")
            task_manager.update_status(task_id, TaskStatus.CANCELLED)
        except Exception as e:
            error_msg = str(e)
            task_manager.add_log(task_id, f"爬取失败: {error_msg}", "ERROR")
            task_manager.set_error(task_id, error_msg)
        finally:
            # 移除日志处理器
            try:
                crawler_logger = logging.getLogger('zhipin_crawler')
                crawler_logger.removeHandler(task_handler)
            except:
                pass
            
            # 关闭浏览器
            try:
                crawler.close()
            except:
                pass
                
    except Exception as e:
        error_msg = f"任务执行失败: {str(e)}"
        task_manager.add_log(task_id, error_msg, "ERROR")
        task_manager.set_error(task_id, error_msg)


def start_crawl_task(params: Dict, output_dir: str = "output") -> str:
    """
    启动爬虫任务（异步）
    
    Args:
        params: 爬虫参数
        output_dir: 输出目录
    
    Returns:
        任务ID
    """
    # 创建任务
    task_id = task_manager.create_task("crawl", params)
    
    # 在后台线程中执行
    thread = threading.Thread(
        target=run_crawl_task,
        args=(task_id, params, output_dir),
        daemon=True
    )
    thread.start()
    
    return task_id
