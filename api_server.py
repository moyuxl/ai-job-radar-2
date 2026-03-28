"""
FastAPI 服务器：职位描述深度分析
使用 LLM API 对职位描述进行结构化分析和评分
"""
import os
import json
import re
import pandas as pd
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from pathlib import Path
import logging
import sys
import io
import signal
import threading

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
from openai import OpenAI
from dotenv import load_dotenv

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 加载环境变量
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 全局中断标志（线程安全）
interrupt_flag = threading.Event()

def signal_handler(signum, frame):
    """处理 Ctrl+C 信号"""
    logger.warning("\n收到中断信号 (Ctrl+C)，正在安全退出...")
    interrupt_flag.set()

# 注册信号处理器（Windows 和 Unix 都支持）
if sys.platform == 'win32':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
else:
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

app = FastAPI(title="职位描述深度分析API", version="1.0.0")

# 挂载静态文件（如果需要）
templates_dir = Path(__file__).parent / "templates"
if templates_dir.exists():
    app.mount("/static", StaticFiles(directory=str(templates_dir)), name="static")

# 模型配置（从 .env 加载）
def _get_model_config(model_id: str) -> Tuple[str, str, str]:
    """根据 model_id 返回 (api_key, base_url, model_name)"""
    configs = {
        "supermind": (
            os.getenv("SUPER_MIND_API_KEY"),
            os.getenv("SUPER_MIND_BASE_URL"),
            os.getenv("SUPER_MIND_MODEL"),
        ),
        "deepseek_chat": (
            os.getenv("DEEPSEEK_API_KEY"),
            os.getenv("DEEPSEEK_BASE_URL"),
            os.getenv("DEEPSEEK_MODEL_CHAT", "deepseek-chat"),
        ),
        "deepseek_reasoner": (
            os.getenv("DEEPSEEK_API_KEY"),
            os.getenv("DEEPSEEK_BASE_URL"),
            os.getenv("DEEPSEEK_MODEL_REASONER", "deepseek-reasoner"),
        ),
    }
    cfg = configs.get(model_id)
    if not cfg or not all(cfg):
        raise ValueError(f"模型 {model_id} 未在 .env 中完整配置")
    return cfg


def get_available_models() -> List[Dict[str, str]]:
    """返回已配置的模型列表（默认优先 DeepSeek Chat）"""
    models = []
    if all(os.getenv(k) for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")):
        models.append({"id": "deepseek_chat", "name": "DeepSeek Chat"})
        models.append({"id": "deepseek_reasoner", "name": "DeepSeek Reasoner"})
    if all(os.getenv(k) for k in ("SUPER_MIND_API_KEY", "SUPER_MIND_BASE_URL", "SUPER_MIND_MODEL")):
        models.append({"id": "supermind", "name": "Supermind"})
    return models


# 默认客户端（使用第一个已配置的模型）。无配置时不抛错，便于 Docker/云上先启动进程（用到 LLM 时再报错）。
_available = get_available_models()
if not _available:
    logger.warning(
        "未配置任何 LLM（需 DEEPSEEK_API_KEY + DEEPSEEK_BASE_URL，或 Supermind 的 KEY/BASE_URL/MODEL）；"
        "服务可启动，分析/匹配等依赖模型的功能将不可用。"
    )
    _default_model_id = None
    client = None
else:
    _default_model_id = _available[0]["id"]
    _default_cfg = _get_model_config(_default_model_id)
    client = OpenAI(api_key=_default_cfg[0], base_url=_default_cfg[1], timeout=120)


class AnalysisRequest(BaseModel):
    """分析请求模型"""
    excel_path: Optional[str] = None


class AnalysisResponse(BaseModel):
    """分析响应模型"""
    success: bool
    message: str
    output_file: Optional[str] = None
    total_jobs: int = 0
    analyzed_jobs: int = 0
    total_tokens: Optional[int] = None
    total_input_tokens: Optional[int] = None
    total_output_tokens: Optional[int] = None


def format_work_content_to_text(work_content: List[Dict]) -> str:
    """
    将工作内容列表格式化为带序号的文本
    
    Args:
        work_content: [{"task": "...", "deliverable": "..."}, ...]
    
    Returns:
        格式化的文本，如：
        1. 开发AI产品（交付物：产品原型）
        2. 优化算法性能（交付物：性能报告）
    """
    if not work_content:
        return ""
    
    lines = []
    for idx, item in enumerate(work_content, 1):
        if isinstance(item, dict):
            task = item.get("task", "未明确")
            deliverable = item.get("deliverable", "未明确")
            if deliverable != "未明确":
                lines.append(f"{idx}. {task}（交付物：{deliverable}）")
            else:
                lines.append(f"{idx}. {task}")
        else:
            lines.append(f"{idx}. {str(item)}")
    
    return "\n".join(lines)


def format_skills_to_text(skills: List[str]) -> str:
    """
    将技能列表格式化为带序号的文本
    
    Args:
        skills: ["技能1", "技能2", ...]
    
    Returns:
        格式化的文本，如：
        1. Python
        2. 机器学习
    """
    if not skills:
        return ""
    
    lines = []
    for idx, skill in enumerate(skills, 1):
        lines.append(f"{idx}. {skill}")
    
    return "\n".join(lines)


def extract_json_from_text(text: str, aggressive_repair: bool = False) -> Dict:
    """
    从文本中提取 JSON（去除 markdown 代码块、前后缀等）
    
    Args:
        text: 包含 JSON 的文本
        aggressive_repair: 是否使用激进的修复策略（用于重试）
    """
    if not text or not isinstance(text, str):
        raise ValueError("输入文本为空或格式错误")
    
    # 移除 markdown 代码块标记
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
    
    # 查找 JSON 对象（从第一个 { 到最后一个 }）
    start_idx = text.find('{')
    end_idx = text.rfind('}')
    
    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        raise ValueError("未找到有效的 JSON 对象")
    
    json_str = text[start_idx:end_idx + 1]
    
    # 尝试解析 JSON
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        # 尝试修复常见的 JSON 问题
        # 1. 移除尾随逗号
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        # 2. 激进的修复策略（用于重试）
        if aggressive_repair:
            # 修复单引号（替换为双引号）
            json_str = re.sub(r"'([^']*)':", r'"\1":', json_str)
            json_str = re.sub(r":\s*'([^']*)'", r': "\1"', json_str)
            # 修复未转义的控制字符
            json_str = json_str.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
            # 尝试修复不完整的字符串
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)
        
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            logger.error(f"JSON 解析失败: {e}")
            logger.error(f"提取的文本（前500字符）: {json_str[:500]}")
            raise ValueError(f"JSON 解析失败: {e}")


def is_analysis_result_valid(analysis_result: Dict) -> Tuple[bool, str]:
    """
    检查分析结果是否有效
    
    Returns:
        Tuple[是否有效, 错误信息]
    """
    work_content = analysis_result.get("work_content", [])
    must_have_skills = analysis_result.get("must_have_skills", [])
    
    # 检查工作内容或必备技能是否为空
    if not work_content or len(work_content) == 0:
        return False, "工作内容为空"
    
    if not must_have_skills or len(must_have_skills) == 0:
        return False, "必备技能为空"
    
    # 检查工作内容是否都是空值
    if all(not item or (isinstance(item, dict) and not item.get("task")) for item in work_content):
        return False, "工作内容均为空值"
    
    return True, ""


def call_llm_analyze_with_retry(job_desc: str, max_retries: int = 1, model_id: Optional[str] = None) -> Tuple[Dict, Dict, str]:
    """
    调用 LLM API 分析职位描述，带重试机制
    
    Args:
        job_desc: 职位描述文本
        max_retries: 最大重试次数（默认1次）
        model_id: 模型 ID（supermind/deepseek），None 时使用默认
    
    Returns:
        Tuple[分析结果字典, token使用信息字典, 错误信息]
        如果成功，错误信息为空字符串
    """
    error_msg = ""
    analysis_result = {}
    token_info = {}
    
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                logger.warning(f"第 {attempt + 1} 次重试分析（JSON 修复模式）...")
            
            # 重试时使用激进修复模式
            analysis_result, token_info = call_llm_analyze(job_desc, aggressive_repair=(attempt > 0), model_id=model_id)
            
            # 检查结果是否有效
            is_valid, validation_error = is_analysis_result_valid(analysis_result)
            
            if is_valid:
                return analysis_result, token_info, ""
            else:
                if attempt < max_retries:
                    logger.warning(f"分析结果无效（{validation_error}），准备重试...")
                    error_msg = validation_error
                    continue
                else:
                    return analysis_result, token_info, f"重试后仍无效: {validation_error}"
                    
        except Exception as e:
            error_msg = str(e)
            if attempt < max_retries:
                logger.warning(f"分析失败（{error_msg}），准备重试...")
                continue
            else:
                return {
                    "work_content": [],
                    "must_have_skills": [],
                    "nice_to_have_skills": [],
                    "signals": {
                        "deliverables": [],
                        "process_terms": [],
                        "metrics_terms": [],
                        "fluff_terms": []
                    },
                    "evidence_snippets": []
                }, {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }, f"重试后仍失败: {error_msg}"
    
    return analysis_result, token_info, error_msg


def call_llm_analyze(job_desc: str, aggressive_repair: bool = False, model_id: Optional[str] = None) -> Tuple[Dict, Dict]:
    """
    调用 LLM API 分析职位描述
    
    Args:
        job_desc: 职位描述文本
        aggressive_repair: 是否使用激进的 JSON 修复策略（用于重试）
        model_id: 模型 ID（supermind/deepseek），None 时使用默认
    
    Returns:
        Tuple[分析结果字典, token使用信息字典]
        token使用信息包含: prompt_tokens, completion_tokens, total_tokens
    """
    prompt = f"""你是一个专业的职位描述分析专家。请分析以下职位描述（JD），并严格按照要求输出 JSON 格式结果。

【重要要求】
1. 必须只输出严格可解析的 JSON（UTF-8），不要输出任何解释、前后缀、代码块、markdown、或多余文字。
2. JSON 必须包含所有要求字段；缺失信息时用空数组或空字符串，不要省略字段。
3. 不得编造不存在的事实；所有结论必须能从输入 JD 文本中找到依据。

【职位描述】
{job_desc}

【提取字段要求】
请输出以下 JSON 结构：

{{
  "work_content": [
    {{"task": "动词短语", "deliverable": "交付物/产出"}},
    ...
  ],
  "must_have_skills": ["技能1", "技能2", ...],
  "nice_to_have_skills": ["技能1", "技能2", ...],
  "signals": {{
    "deliverables": ["关键词1", "关键词2", ...],
    "process_terms": ["关键词1", "关键词2", ...],
    "metrics_terms": ["关键词1", "关键词2", ...],
    "fluff_terms": ["关键词1", "关键词2", ...]
  }},
  "evidence_snippets": ["原文短句1", "原文短句2", ...]
}}

【字段说明】
1) work_content：列出3-8条"每天做什么"，每条必须包含 task（动词短语）+ deliverable（交付物/产出），如果JD没有写清楚，用"未明确"但尽量从原文推断，不要编造不存在的技术栈。
2) must_have_skills：列出4-10条硬性要求技能（具体技术/经验/能力），不要写空话。
3) nice_to_have_skills：列出0-6条加分项技能（若没有明确提及则为空数组）。
4) signals：从原文提取关键词短语
   - deliverables（如：pipeline/API/dashboard/eval report/方案/文档/指标）
   - process_terms（如：评测/monitoring/A-B/迭代/成本/延迟/上线）
   - metrics_terms（如：准确率/召回/SLA/latency/cost）
   - fluff_terms（如：自驱/抗压/沟通/热爱/激情/学习能力强）
5) evidence_snippets：从原文摘取1-3条短句作为证据（尽量原文，短一点）

现在请直接输出 JSON，不要任何其他文字："""

    # 根据 model_id 选择客户端
    mid = model_id or _default_model_id
    if not mid:
        raise ValueError(
            "未配置 LLM：请在环境变量中设置 DEEPSEEK_API_KEY 与 DEEPSEEK_BASE_URL（或 Supermind 的 SUPER_MIND_*）。"
        )
    api_key, base_url, model_name = _get_model_config(mid)
    _client = OpenAI(api_key=api_key, base_url=base_url, timeout=120)

    try:
        # 尝试使用 JSON 模式（如果模型支持）
        try:
            response = _client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个专业的职位描述分析专家。你必须只输出严格可解析的 JSON，不要输出任何解释、前后缀、代码块、markdown、或多余文字。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
        except Exception as e:
            # 如果 JSON 模式不支持，使用普通模式
            logger.warning(f"JSON 模式不支持，使用普通模式: {e}")
            response = _client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是一个专业的职位描述分析专家。你必须只输出严格可解析的 JSON，不要输出任何解释、前后缀、代码块、markdown、或多余文字。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
        
        content = response.choices[0].message.content.strip()
        logger.debug(f"LLM 原始响应: {content[:200]}...")
        
        # 提取 token 使用信息
        token_info = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
        if hasattr(response, 'usage') and response.usage:
            token_info["prompt_tokens"] = getattr(response.usage, 'prompt_tokens', 0) or 0
            token_info["completion_tokens"] = getattr(response.usage, 'completion_tokens', 0) or 0
            token_info["total_tokens"] = getattr(response.usage, 'total_tokens', 0) or 0
        
        # 提取 JSON（如果失败，抛出异常让重试机制处理）
        try:
            result = extract_json_from_text(content, aggressive_repair=aggressive_repair)
        except (ValueError, json.JSONDecodeError) as e:
            # JSON 解析错误，抛出异常让重试机制处理
            logger.warning(f"JSON 解析失败（将触发重试）: {e}")
            raise
        
        # 验证必需字段
        required_fields = ["work_content", "must_have_skills", "nice_to_have_skills", "signals", "evidence_snippets"]
        for field in required_fields:
            if field not in result:
                result[field] = [] if field != "signals" else {}
        
        if "signals" not in result or not isinstance(result["signals"], dict):
            result["signals"] = {
                "deliverables": [],
                "process_terms": [],
                "metrics_terms": [],
                "fluff_terms": []
            }
        
        return result, token_info
        
    except (ValueError, json.JSONDecodeError) as e:
        # JSON 解析错误，重新抛出让重试机制处理
        logger.warning(f"JSON 解析错误（将触发重试）: {e}")
        raise
    except Exception as e:
        # 其他错误（如 API 调用失败），记录并返回空结构
        logger.error(f"LLM API 调用失败: {e}")
        # 返回空结构和空的 token 信息
        return {
            "work_content": [],
            "must_have_skills": [],
            "nice_to_have_skills": [],
            "signals": {
                "deliverables": [],
                "process_terms": [],
                "metrics_terms": [],
                "fluff_terms": []
            },
            "evidence_snippets": []
        }, {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }


def calculate_scores(analysis_result: Dict) -> Dict:
    """
    计算评分
    
    评分规则：
    A) completeness 0-100：
    - work_content 条目数：>=5 得40；3-4 得28；1-2 得16；0 得0
    - must_have_skills 条目数：>=6 得40；4-5 得28；1-3 得16；0 得0
    - nice_to_have_skills 条目数：>=3 得20；1-2 得12；0 得4
    completeness = 三项相加

    B) actionability 0-30（只加分）：
    如果 deliverables 非空 +10（最多10）
    如果 process_terms 非空 +10（最多10）
    如果 metrics_terms 非空 +10（最多10）
    actionability = 三项相加

    total = completeness + actionability
    """
    work_content_count = len(analysis_result.get("work_content", []))
    must_have_count = len(analysis_result.get("must_have_skills", []))
    nice_to_have_count = len(analysis_result.get("nice_to_have_skills", []))
    
    signals = analysis_result.get("signals", {})
    deliverables = signals.get("deliverables", [])
    process_terms = signals.get("process_terms", [])
    metrics_terms = signals.get("metrics_terms", [])
    fluff_terms = signals.get("fluff_terms", [])
    
    # A) completeness
    # work_content
    if work_content_count >= 5:
        work_content_score = 40
    elif work_content_count >= 3:
        work_content_score = 28
    elif work_content_count >= 1:
        work_content_score = 16
    else:
        work_content_score = 0
    
    # must_have_skills
    if must_have_count >= 6:
        must_have_score = 40
    elif must_have_count >= 4:
        must_have_score = 28
    elif must_have_count >= 1:
        must_have_score = 16
    else:
        must_have_score = 0
    
    # nice_to_have_skills
    if nice_to_have_count >= 3:
        nice_to_have_score = 20
    elif nice_to_have_count >= 1:
        nice_to_have_score = 12
    else:
        nice_to_have_score = 4
    
    completeness = work_content_score + must_have_score + nice_to_have_score
    
    # B) actionability
    actionability = 0
    if deliverables:
        actionability += 10
    if process_terms:
        actionability += 10
    if metrics_terms:
        actionability += 10
    
    total_score = completeness + actionability
    
    # Flags
    thin_jd = completeness < 45
    fluffy = len(fluff_terms) >= 4 and actionability <= 10
    needs_manual_review = (
        len(analysis_result.get("evidence_snippets", [])) == 0 or
        work_content_count < 2 or
        must_have_count < 2
    )
    
    # rank_reason
    reasons = []
    if completeness >= 80:
        reasons.append(f"信息完整度高（completeness={completeness}），工作内容和技能要求清晰")
    elif completeness < 45:
        reasons.append(f"信息不完整（completeness={completeness}），缺少关键信息")
    
    if actionability >= 20:
        reasons.append(f"可执行性强（actionability={actionability}），包含明确的交付物、流程和指标")
    elif actionability <= 10:
        reasons.append(f"可执行性弱（actionability={actionability}），缺少具体的交付物和流程")
    
    if fluffy:
        reasons.append("包含较多空话（fluff_terms较多），实际可执行信息不足")
    
    if not reasons:
        reasons.append(f"综合评分 {total_score}，信息完整度 {completeness}，可执行性 {actionability}")
    
    return {
        "completeness": completeness,
        "actionability": actionability,
        "total": total_score,
        "flags": {
            "thin_jd": thin_jd,
            "fluffy": fluffy,
            "needs_manual_review": needs_manual_review
        },
        "rank_reason": "；".join(reasons[:4])  # 最多4条
    }


def analyze_excel_file(excel_path: str) -> Tuple[str, Dict]:
    """
    分析 Excel 文件中的所有职位描述
    
    Returns:
        Tuple[输出文件路径, token统计信息]
        token统计信息包含: total_tokens, total_input_tokens, total_output_tokens
    """
    # 重置中断标志
    interrupt_flag.clear()
    
    logger.info(f"开始分析 Excel 文件: {excel_path}")
    logger.info("提示：按 Ctrl+C 可以中断分析并保存已处理的数据")
    
    # 读取 Excel
    df = pd.read_excel(excel_path)
    logger.info(f"读取到 {len(df)} 条岗位数据")
    
    # 检查是否有职位描述列
    if '职位描述' not in df.columns:
        raise ValueError("Excel文件中没有找到'职位描述'列")
    
    # Token 统计
    total_input_tokens = 0
    total_output_tokens = 0
    total_tokens = 0
    
    # 分析每条岗位
    results = []
    processed_count = 0
    
    try:
        for idx, row in df.iterrows():
            # 检查中断标志
            if interrupt_flag.is_set():
                logger.warning(f"\n检测到中断信号，已处理 {processed_count}/{len(df)} 条，正在保存已处理的数据...")
                break
            
            job_desc = str(row.get('职位描述', ''))
            if not job_desc or job_desc == 'nan':
                logger.warning(f"第 {idx+1} 行职位描述为空，跳过")
                continue
            
            logger.info(f"正在分析第 {idx+1}/{len(df)} 条岗位...")
            
            try:
                # 调用 LLM 分析（带重试机制）
                analysis_result, token_info, analysis_error = call_llm_analyze_with_retry(job_desc, max_retries=1)
                
                # 累计 token 统计
                prompt_tokens = token_info.get("prompt_tokens", 0)
                completion_tokens = token_info.get("completion_tokens", 0)
                tokens = token_info.get("total_tokens", 0)
                
                total_input_tokens += prompt_tokens
                total_output_tokens += completion_tokens
                total_tokens += tokens
                
                # 记录日志
                logger.info(
                    f"第 {idx+1} 条分析完成 - "
                    f"输入token: {prompt_tokens}, "
                    f"输出token: {completion_tokens}, "
                    f"总token: {tokens}"
                )
                
                # 检查是否有错误
                has_error = bool(analysis_error)
                
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
                
                # 添加条目数统计（方便查看和筛选）
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
                
                # 计算细分评分（无上限，用于打破满分扎堆）
                # 细分评分 = 2×工作内容条目数 + 1×必备技能条目数 + 1×加分技能条目数 + 1×交付物数量 + 1×流程词数量
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
                
                # 如果有错误，标记为错误而不是"岗位信息不足"
                if has_error:
                    result_row['分析错误'] = analysis_error
                    result_row['标记-信息不足'] = False  # 强制设为 False，避免误判
                    result_row['标记-需人工审核'] = True  # 标记为需人工审核
                    logger.error(f"第 {idx+1} 条分析失败: {analysis_error}")
                else:
                    result_row['分析错误'] = ""  # 空字符串表示无错误
                    result_row['标记-信息不足'] = scores["flags"]["thin_jd"]
                    result_row['标记-需人工审核'] = scores["flags"]["needs_manual_review"]
                
                result_row['标记-空话多'] = scores["flags"]["fluffy"]
                result_row['评分理由'] = scores["rank_reason"]
                
                # 添加 token 信息到 Excel（可选）
                result_row['输入Token'] = prompt_tokens
                result_row['输出Token'] = completion_tokens
                result_row['总Token'] = tokens
                
                results.append(result_row)
                processed_count += 1
                
                # 再次检查中断标志（在处理完一条后）
                if interrupt_flag.is_set():
                    logger.warning(f"\n检测到中断信号，已处理 {processed_count}/{len(df)} 条，正在保存已处理的数据...")
                    break
                
            except KeyboardInterrupt:
                logger.warning(f"\n收到键盘中断，已处理 {processed_count}/{len(df)} 条，正在保存已处理的数据...")
                interrupt_flag.set()
                break
            except Exception as e:
                logger.error(f"分析第 {idx+1} 条岗位时出错: {e}")
                # 保留原始数据，但标记为分析失败
                result_row = row.to_dict()
                result_row['分析状态'] = f'失败: {str(e)}'
                results.append(result_row)
                processed_count += 1
                
    except KeyboardInterrupt:
        logger.warning(f"\n收到键盘中断，已处理 {processed_count}/{len(df)} 条，正在保存已处理的数据...")
        interrupt_flag.set()
    
    # 检查是否有数据需要保存
    if not results:
        raise ValueError("没有处理任何数据，无法生成结果文件")
    
    # 创建结果 DataFrame
    result_df = pd.DataFrame(results)
    
    # 如果被中断，添加提示信息
    if interrupt_flag.is_set() and processed_count < len(df):
        logger.warning(f"分析被中断，仅保存了 {processed_count}/{len(df)} 条数据")
    
    # 按综合评分排序（降序）
    if '综合评分' in result_df.columns:
        result_df = result_df.sort_values('综合评分', ascending=False)
    
    # 生成输出文件名（带时间戳）
    input_path = Path(excel_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"{input_path.stem}_llm_analyzed_{timestamp}.xlsx"
    output_path = input_path.parent / output_filename
    
    # 处理文件权限错误
    counter = 0
    while output_path.exists():
        counter += 1
        output_filename = f"{input_path.stem}_llm_analyzed_{timestamp}_{counter}.xlsx"
        output_path = input_path.parent / output_filename
    
    # 保存到 Excel
    result_df.to_excel(output_path, index=False, engine='openpyxl')
    
    # 记录总 token 统计
    status_msg = "分析完成" if not interrupt_flag.is_set() else f"分析中断（已处理 {processed_count}/{len(df)} 条）"
    logger.info(
        f"{status_msg}！结果已保存到: {output_path.absolute()}\n"
        f"Token 统计 - 总输入token: {total_input_tokens}, "
        f"总输出token: {total_output_tokens}, "
        f"总token: {total_tokens}"
    )
    
    token_stats = {
        "total_tokens": total_tokens,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens
    }
    
    # 重置中断标志（为下次分析做准备）
    interrupt_flag.clear()
    
    return str(output_path.absolute()), token_stats


@app.get("/", response_class=HTMLResponse)
async def root():
    """根路径 - 返回 Web 界面"""
    html_file = templates_dir / "index.html"
    if html_file.exists():
        with open(html_file, "r", encoding="utf-8") as f:
            return f.read()
    return """
    <html>
        <body>
            <h1>职位描述深度分析API</h1>
            <p>请访问 <a href="/docs">/docs</a> 查看 API 文档</p>
        </body>
    </html>
    """


@app.post("/analyze", response_model=AnalysisResponse)
async def analyze_uploaded_file(file: UploadFile = File(...)):
    """
    上传 Excel 文件进行分析
    支持 Ctrl+C 中断，会保存已处理的数据
    """
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="只支持 Excel 文件 (.xlsx, .xls)")
    
    # 保存临时文件
    temp_dir = Path("temp_uploads")
    temp_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_file = temp_dir / f"upload_{timestamp}_{file.filename}"
    
    try:
        # 保存上传的文件
        with open(temp_file, "wb") as f:
            content = await file.read()
            f.write(content)
        
        logger.info(f"文件已上传: {temp_file}")
        
        # 分析文件（支持中断）
        try:
            output_path, token_stats = analyze_excel_file(str(temp_file))
            
            is_interrupted = interrupt_flag.is_set()
            message = "分析完成" if not is_interrupted else "分析被中断，已保存已处理的数据"
            
            return AnalysisResponse(
                success=True,
                message=message,
                output_file=output_path,
                total_jobs=0,  # 可以从分析结果中获取
                analyzed_jobs=0,
                total_tokens=token_stats.get("total_tokens"),
                total_input_tokens=token_stats.get("total_input_tokens"),
                total_output_tokens=token_stats.get("total_output_tokens")
            )
        except KeyboardInterrupt:
            logger.warning("收到键盘中断信号")
            raise HTTPException(status_code=499, detail="分析被用户中断")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"分析失败: {e}")
        raise HTTPException(status_code=500, detail=f"分析失败: {str(e)}")
    
    finally:
        # 清理临时文件
        if temp_file.exists():
            try:
                temp_file.unlink()
            except:
                pass


@app.get("/analyze_file", response_model=AnalysisResponse)
async def analyze_file_path(file_path: str):
    """
    分析指定路径的 Excel 文件
    使用查询参数: /analyze_file?file_path=xxx
    支持 Ctrl+C 中断，会保存已处理的数据
    """
    if not file_path:
        raise HTTPException(status_code=400, detail="请提供 file_path 参数")
    
    # URL 解码
    import urllib.parse
    file_path = urllib.parse.unquote(file_path)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")
    
    if not file_path.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="只支持 Excel 文件 (.xlsx, .xls)")
    
    try:
        try:
            output_path, token_stats = analyze_excel_file(file_path)
            
            is_interrupted = interrupt_flag.is_set()
            message = "分析完成" if not is_interrupted else "分析被中断，已保存已处理的数据"
            
            return AnalysisResponse(
                success=True,
                message=message,
                output_file=output_path,
                total_jobs=0,
                analyzed_jobs=0,
                total_tokens=token_stats.get("total_tokens"),
                total_input_tokens=token_stats.get("total_input_tokens"),
                total_output_tokens=token_stats.get("total_output_tokens")
            )
        except KeyboardInterrupt:
            logger.warning("收到键盘中断信号")
            raise HTTPException(status_code=499, detail="分析被用户中断")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"分析失败: {e}")
        raise HTTPException(status_code=500, detail=f"分析失败: {str(e)}")


@app.get("/download")
async def download_file(file_path: str):
    """
    下载分析结果文件
    使用查询参数: /download?file_path=xxx
    """
    if not file_path:
        raise HTTPException(status_code=400, detail="请提供 file_path 参数")
    
    # URL 解码
    import urllib.parse
    file_path = urllib.parse.unquote(file_path)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")
    
    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=os.path.basename(file_path)
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
