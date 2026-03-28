"""
岗位列表语义过滤：在列表页爬取后、详情页爬取前，使用 LLM 判断岗位与搜索目标的匹配度，
剔除销售、英文产品经理、芯片工程师等不相关岗位。
"""
import json
import re
import logging
from typing import Dict, List, Optional, Tuple

from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
logger = logging.getLogger(__name__)

# 批量大小
BATCH_SIZE = 18


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


def _get_default_model_id() -> str:
    """返回默认模型 ID（优先 DeepSeek Chat）"""
    if all(os.getenv(k) for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")):
        return "deepseek_chat"
    if all(os.getenv(k) for k in ("SUPER_MIND_API_KEY", "SUPER_MIND_BASE_URL", "SUPER_MIND_MODEL")):
        return "supermind"
    raise ValueError("请在 .env 中至少配置 DeepSeek 或 Supermind 的 API_KEY、BASE_URL、MODEL")


def _extract_json_from_text(text: str) -> Dict:
    """从文本中提取 JSON"""
    if not text or not isinstance(text, str):
        raise ValueError("输入文本为空或格式错误")
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
    start_idx = text.find('{')
    end_idx = text.rfind('}')
    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        raise ValueError("未找到有效的 JSON 对象")
    json_str = text[start_idx:end_idx + 1]
    json_str = re.sub(r',\s*}', '}', json_str)
    json_str = re.sub(r',\s*]', ']', json_str)
    return json.loads(json_str)


def _format_job_for_prompt(job: Dict, index: int) -> str:
    """将岗位格式化为 Prompt 中的一行"""
    name = job.get('岗位名称', '') or ''
    tags = job.get('职位标签', '') or ''
    skills = job.get('职位要求', '') or ''
    industry = job.get('公司行业', '') or ''
    company = job.get('公司名称', '') or ''
    return f"[{index}] 岗位名称: {name} | 职位标签: {tags} | 职位要求: {skills} | 公司行业: {industry} | 公司: {company}"


def _call_llm_filter_batch(
    keyword: str,
    jobs_batch: List[Dict],
    start_index: int,
    model_id: str,
) -> Tuple[List[Tuple[int, bool, str]], Dict]:
    """
    对一批岗位调用 LLM 进行过滤判断
    
    Returns:
        Tuple of (List of (original_index, keep, reason), token_info dict)
    """
    model_id = model_id or _get_default_model_id()
    api_key, base_url, model_name = _get_model_config(model_id)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)

    jobs_text = "\n".join(
        _format_job_for_prompt(job, start_index + i)
        for i, job in enumerate(jobs_batch)
    )

    prompt = f"""你是一个职位匹配专家。用户搜索的目标岗位是「{keyword}」。
请判断以下岗位列表中，哪些岗位与目标岗位高度相关（涉及目标领域/行业），哪些不相关。

只根据岗位名称、职位标签、职位要求、公司行业判断，无需查看职位描述。

【判断规则】
- 保留：与目标岗位语义高度相关的岗位（如搜索「AI产品经理」则保留 AI/大模型/LLM/人工智能 相关产品经理）
- 删除：销售、英文/海外产品经理（无目标领域）、芯片/硬件工程师、产品规划专员（无目标领域）、纯贸易/电商等与目标无关的岗位

【示例】目标：AI 产品经理
- 岗位：AI产品经理 | 标签：人工智能,产品 | 要求：AI产品 | 行业：互联网 → 保留
- 岗位：销售经理 | 标签：销售,客户 | 行业：贸易 → 删除
- 岗位：英文产品经理 | 标签：海外,英语 | 行业：跨境电商 → 删除（无 AI）
- 岗位：芯片工程师 | 标签：硬件,芯片 | 行业：半导体 → 删除
- 岗位：产品规划专员 | 标签：需求分析 | 行业：制造业 → 删除（无 AI）

【岗位列表】
{jobs_text}

【输出格式】必须只输出严格可解析的 JSON，不要任何其他文字：
{{"results": [{{"index": 0, "keep": true, "reason": "AI产品相关"}}, {{"index": 1, "keep": false, "reason": "销售岗位"}}, ...]}}

注意：index 对应上面每行的 [{start_index}]、[{start_index+1}] 等序号，keep 为 true 表示保留、false 表示删除。"""

    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "你是一个职位匹配专家。你必须只输出严格可解析的 JSON，不要输出任何解释、前后缀、代码块、markdown、或多余文字。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
        )
        content = response.choices[0].message.content.strip()
        token_info = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        if hasattr(response, "usage") and response.usage:
            token_info["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0) or 0
            token_info["completion_tokens"] = getattr(response.usage, "completion_tokens", 0) or 0
            token_info["total_tokens"] = getattr(response.usage, "total_tokens", 0) or 0

        data = _extract_json_from_text(content)
        results = data.get("results", [])
        if not results:
            # 解析失败或无结果：默认全部保留
            return [(start_index + i, True, "解析无结果，默认保留") for i in range(len(jobs_batch))], token_info

        out = []
        for i, job in enumerate(jobs_batch):
            orig_idx = start_index + i
            # 查找对应结果（按 index 或顺序）
            r = next((x for x in results if x.get("index") == orig_idx or x.get("index") == i), None)
            if r is None:
                r = results[i] if i < len(results) else {}
            keep = r.get("keep", True) if isinstance(r.get("keep"), bool) else str(r.get("keep", "true")).lower() in ("true", "1", "yes", "保留")
            reason = str(r.get("reason", "")) or ("保留" if keep else "删除")
            out.append((orig_idx, keep, reason))
        return out, token_info
    except Exception as e:
        logger.warning(f"LLM 过滤批次失败: {e}，该批次默认全部保留")
        return [(start_index + i, True, f"LLM 调用失败，默认保留: {e}") for i in range(len(jobs_batch))], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def filter_jobs_by_semantic_match(
    keyword: str,
    jobs: List[Dict],
    model_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> List[Dict]:
    """
    使用 LLM 对岗位列表进行语义过滤，剔除与搜索目标不相关的岗位。

    Args:
        keyword: 搜索目标岗位（如「AI 产品经理」）
        jobs: 列表页岗位数据
        model_id: 模型 ID（supermind/deepseek_chat/deepseek_reasoner），None 时使用默认
        task_id: 任务 ID，用于写入 task_manager 日志（可选）

    Returns:
        过滤后的岗位列表（仅保留匹配的岗位）。若 LLM 调用失败，返回原列表（安全降级）。
    """
    if not jobs:
        return jobs

    def log(msg: str, level: str = "INFO"):
        logger.info(msg) if level == "INFO" else logger.warning(msg)
        if task_id:
            try:
                from task_manager import task_manager
                task_manager.add_log(task_id, msg, level)
            except Exception:
                pass

    log(f"开始 LLM 语义过滤：目标岗位「{keyword}」，共 {len(jobs)} 条", "INFO")

    try:
        model_id = model_id or _get_default_model_id()
    except ValueError as e:
        log(f"模型配置错误，跳过过滤: {e}", "WARNING")
        return jobs

    keep_indices = set()
    all_results = []
    total_prompt_tokens = 0
    total_completion_tokens = 0

    for start in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[start:start + BATCH_SIZE]
        batch_results, token_info = _call_llm_filter_batch(keyword, batch, start, model_id)
        all_results.extend(batch_results)
        total_prompt_tokens += token_info.get("prompt_tokens", 0)
        total_completion_tokens += token_info.get("completion_tokens", 0)
        for orig_idx, keep, reason in batch_results:
            if keep:
                keep_indices.add(orig_idx)
        log(f"过滤批次 {start // BATCH_SIZE + 1}: 已处理 {min(start + BATCH_SIZE, len(jobs))}/{len(jobs)} 条", "INFO")

    filtered = [jobs[i] for i in range(len(jobs)) if i in keep_indices]
    removed = len(jobs) - len(filtered)
    total_tokens = total_prompt_tokens + total_completion_tokens
    log(f"LLM 过滤完成：原 {len(jobs)} 条 → 保留 {len(filtered)} 条，删除 {removed} 条 | Token 消耗: 输入 {total_prompt_tokens}, 输出 {total_completion_tokens}, 总计 {total_tokens}", "INFO")

    for orig_idx, keep, reason in all_results:
        if not keep:
            job_name = jobs[orig_idx].get('岗位名称', '') if orig_idx < len(jobs) else ''
            log(f"  删除: [{orig_idx}] {job_name} - {reason}", "INFO")

    return filtered
