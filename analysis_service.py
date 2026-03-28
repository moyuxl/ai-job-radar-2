"""
分析服务：封装 LLM 分析功能，支持后台任务执行和状态更新
"""
import os
import json
import threading
import logging
import time
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime

from api_server import analyze_excel_file, call_llm_analyze_with_retry, calculate_scores, format_work_content_to_text, format_skills_to_text
from task_manager import task_manager, TaskStatus
from task_log_handler import TaskLogHandler
import pandas as pd

logger = logging.getLogger(__name__)


def run_analysis_task(task_id: str, excel_path: str, output_dir: str = "output", model_id: str = ""):
    """
    在后台线程中执行分析任务
    
    Args:
        task_id: 任务ID
        excel_path: 原始数据 Excel 文件路径
        output_dir: 输出目录
        model_id: 模型 ID（supermind/deepseek），空时使用默认
    """
    try:
        # 更新状态为运行中
        task_manager.update_status(task_id, TaskStatus.RUNNING)
        task_manager.add_log(task_id, "开始分析任务", "INFO")
        
        # 验证文件是否存在
        excel_path_obj = Path(excel_path)
        if not excel_path_obj.exists():
            error_msg = f"文件不存在: {excel_path}"
            task_manager.add_log(task_id, error_msg, "ERROR")
            task_manager.set_error(task_id, error_msg)
            return
        
        # 创建输出目录
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # 设置日志处理器，将分析日志重定向到任务管理器
        task_handler = TaskLogHandler(task_id=task_id)
        task_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(message)s')
        task_handler.setFormatter(formatter)
        
        # 获取分析相关的日志记录器并添加处理器
        analysis_logger = logging.getLogger('api_server')
        analysis_logger.addHandler(task_handler)
        analysis_logger.setLevel(logging.INFO)
        
        # 读取 Excel 文件
        task_manager.add_log(task_id, f"正在读取文件: {excel_path}", "INFO")
        df = pd.read_excel(excel_path)
        total_jobs = len(df)
        task_manager.add_log(task_id, f"读取到 {total_jobs} 条岗位数据", "INFO")
        
        # 检查是否有职位描述列
        if '职位描述' not in df.columns:
            error_msg = "Excel文件中没有找到'职位描述'列"
            task_manager.add_log(task_id, error_msg, "ERROR")
            task_manager.set_error(task_id, error_msg)
            return
        
        # 初始化统计信息
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        success_count = 0
        failed_count = 0
        
        # 分析每条岗位
        results = []
        processed_count = 0
        
        # 更新进度
        task_manager.update_progress(task_id, 0, total_jobs)
        
        try:
            for idx, row in df.iterrows():
                job_desc = str(row.get('职位描述', ''))
                if not job_desc or job_desc == 'nan':
                    task_manager.add_log(task_id, f"第 {idx+1} 行职位描述为空，跳过", "WARNING")
                    continue
                
                # 更新进度和日志（每5条更新一次，或者最后一条）
                if (idx + 1) % 5 == 0 or idx == len(df) - 1:
                    task_manager.add_log(task_id, f"正在分析第 {idx+1}/{total_jobs} 条岗位...", "INFO")
                    task_manager.update_progress(task_id, idx + 1, total_jobs)
                
                try:
                    # 每条请求前等待 2 秒（首条除外），避免 API 速率限制（约 30 次/分钟）
                    if results:
                        time.sleep(2)
                    # 调用 LLM 分析（带重试机制）
                    analysis_result, token_info, analysis_error = call_llm_analyze_with_retry(
                        job_desc, max_retries=1, model_id=model_id or None
                    )
                    
                    # 累计 token 统计
                    prompt_tokens = token_info.get("prompt_tokens", 0)
                    completion_tokens = token_info.get("completion_tokens", 0)
                    tokens = token_info.get("total_tokens", 0)
                    
                    total_input_tokens += prompt_tokens
                    total_output_tokens += completion_tokens
                    total_tokens += tokens
                    
                    # 记录日志（每条都记录 token）
                    task_manager.add_log(
                        task_id,
                        f"第 {idx+1} 条分析完成 - 输入token: {prompt_tokens}, 输出token: {completion_tokens}, 总token: {tokens}",
                        "INFO"
                    )
                    
                    # 检查是否有错误
                    has_error = bool(analysis_error)
                    
                    if has_error:
                        failed_count += 1
                    else:
                        success_count += 1
                    
                    # 计算评分
                    scores = calculate_scores(analysis_result)
                    
                    # 合并原始数据和分析结果
                    result_row = row.to_dict()
                    
                    # 添加提取的字段（格式化为带序号的文本）
                    work_content = analysis_result.get("work_content", [])
                    must_have_skills = analysis_result.get("must_have_skills", [])
                    nice_to_have_skills = analysis_result.get("nice_to_have_skills", [])
                    
                    result_row['工作内容'] = format_work_content_to_text(work_content)
                    result_row['必备技能'] = format_skills_to_text(must_have_skills)
                    result_row['加分技能'] = format_skills_to_text(nice_to_have_skills)
                    
                    # 添加条目数统计
                    result_row['工作内容条目数'] = len(work_content)
                    result_row['必备技能条目数'] = len(must_have_skills)
                    result_row['加分技能条目数'] = len(nice_to_have_skills)
                    
                    # 其他字段保持 JSON 格式（信号词）
                    result_row['信号词-交付物'] = json.dumps(analysis_result.get("signals", {}).get("deliverables", []), ensure_ascii=False)
                    result_row['信号词-流程'] = json.dumps(analysis_result.get("signals", {}).get("process_terms", []), ensure_ascii=False)
                    result_row['信号词-指标'] = json.dumps(analysis_result.get("signals", {}).get("metrics_terms", []), ensure_ascii=False)
                    result_row['信号词-空话'] = json.dumps(analysis_result.get("signals", {}).get("fluff_terms", []), ensure_ascii=False)
                    
                    # 添加评分
                    result_row['信息完整度'] = scores["completeness"]
                    result_row['可执行性'] = scores["actionability"]
                    result_row['综合评分'] = scores["total"]
                    
                    # 计算细分评分
                    deliverables_count = len(analysis_result.get("signals", {}).get("deliverables", []))
                    process_terms_count = len(analysis_result.get("signals", {}).get("process_terms", []))
                    detail_score = (
                        2 * len(work_content) +
                        1 * len(must_have_skills) +
                        1 * len(nice_to_have_skills) +
                        1 * deliverables_count +
                        1 * process_terms_count
                    )
                    result_row['细分评分'] = detail_score
                    
                    # 如果有错误，标记为错误
                    if has_error:
                        result_row['分析错误'] = analysis_error
                        result_row['标记-信息不足'] = False
                        result_row['标记-需人工审核'] = True
                        task_manager.add_log(task_id, f"第 {idx+1} 条分析失败: {analysis_error}", "ERROR")
                    else:
                        result_row['分析错误'] = ""
                        result_row['标记-信息不足'] = scores["flags"]["thin_jd"]
                        result_row['标记-需人工审核'] = scores["flags"]["needs_manual_review"]
                    
                    result_row['标记-空话多'] = scores["flags"]["fluffy"]
                    result_row['评分理由'] = scores["rank_reason"]
                    
                    # 添加 token 信息
                    result_row['输入Token'] = prompt_tokens
                    result_row['输出Token'] = completion_tokens
                    result_row['总Token'] = tokens
                    
                    results.append(result_row)
                    processed_count += 1
                    
                except Exception as e:
                    error_msg = f"分析第 {idx+1} 条岗位时出错: {str(e)}"
                    task_manager.add_log(task_id, error_msg, "ERROR")
                    failed_count += 1
                    # 保留原始数据，但标记为分析失败
                    result_row = row.to_dict()
                    result_row['分析状态'] = f'失败: {str(e)}'
                    result_row['分析错误'] = str(e)
                    results.append(result_row)
                    processed_count += 1
                    continue
        
        except Exception as e:
            error_msg = f"分析过程出错: {str(e)}"
            task_manager.add_log(task_id, error_msg, "ERROR")
            task_manager.set_error(task_id, error_msg)
            return
        
        # 检查是否有数据需要保存
        if not results:
            error_msg = "没有处理任何数据，无法生成结果文件"
            task_manager.add_log(task_id, error_msg, "ERROR")
            task_manager.set_error(task_id, error_msg)
            return
        
        # 创建结果 DataFrame
        result_df = pd.DataFrame(results)
        
        # 按综合评分排序（降序）
        if '综合评分' in result_df.columns:
            result_df = result_df.sort_values('综合评分', ascending=False)
        
        # 生成输出文件名（带时间戳）
        input_path = Path(excel_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"{input_path.stem}_analyzed_{timestamp}.xlsx"
        result_output_path = output_path / output_filename
        
        # 处理文件权限错误
        counter = 0
        while result_output_path.exists():
            counter += 1
            output_filename = f"{input_path.stem}_analyzed_{timestamp}_{counter}.xlsx"
            result_output_path = output_path / output_filename
        
        # 保存到 Excel
        task_manager.add_log(task_id, f"正在保存结果到: {result_output_path}", "INFO")
        result_df.to_excel(result_output_path, index=False, engine='openpyxl')
        
        # 更新结果（使用字典方式更新，支持任意字段）
        result_dict = {
            "success_count": success_count,
            "failed_count": failed_count,
            "output_file": str(result_output_path.absolute()),
            "total_tokens": total_tokens,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens
        }
        task_manager.update_result(task_id, **result_dict)
        
        # 记录完成日志
        task_manager.add_log(
            task_id,
            f"分析完成！成功: {success_count}, 失败: {failed_count}, 总Token: {total_tokens}",
            "INFO"
        )
        task_manager.add_log(task_id, f"结果已保存到: {result_output_path.absolute()}", "INFO")
        
        # 更新状态为完成
        task_manager.update_status(task_id, TaskStatus.COMPLETED)
        
    except KeyboardInterrupt:
        task_manager.add_log(task_id, "用户中断分析", "WARNING")
        task_manager.update_status(task_id, TaskStatus.CANCELLED)
    except Exception as e:
        error_msg = f"分析任务失败: {str(e)}"
        task_manager.add_log(task_id, error_msg, "ERROR")
        task_manager.set_error(task_id, error_msg)
    finally:
        # 移除日志处理器
        try:
            analysis_logger = logging.getLogger('api_server')
            analysis_logger.removeHandler(task_handler)
        except:
            pass


def start_analysis_task(excel_path: str, output_dir: str = "output", model_id: str = "") -> str:
    """
    启动分析任务（异步）
    
    Args:
        excel_path: 原始数据 Excel 文件路径
        output_dir: 输出目录
        model_id: 模型 ID（supermind/deepseek），空时使用默认
    
    Returns:
        任务ID
    """
    # 创建任务
    task_id = task_manager.create_task("analysis", {
        "excel_path": excel_path,
        "output_dir": output_dir,
        "model_id": model_id
    })
    
    # 在后台线程中执行
    thread = threading.Thread(
        target=run_analysis_task,
        args=(task_id, excel_path, output_dir, model_id),
        daemon=True
    )
    thread.start()
    
    return task_id
