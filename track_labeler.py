"""
赛道标注：对 DB 中已有职位描述的岗位打标签（公司属性、岗位实质、置信度）。
独立模块：只读写 jobs.db，与是否生成 Excel、是否刚完成爬取无关；在 Web 上单独触发即可。
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

# 每批发送给 LLM 的岗位条数。
# DeepSeek 官方：上下文长度 128K，输出长度默认 4K、最大 8K。
# 历史取样（每批 6 条示例）：单批 token 约 2k–2.5k 输入（输出远低于 4K）。
# 默认 10 条/批（略减单次体积，便于试跑）；可通过环境变量 TRACK_LABEL_BATCH_SIZE 覆盖（上限 20）。
BATCH_SIZE = int(os.getenv("TRACK_LABEL_BATCH_SIZE", "10"))
if BATCH_SIZE < 1:
    BATCH_SIZE = 1
elif BATCH_SIZE > 20:
    BATCH_SIZE = 20

# 第一维度：公司属性（这家公司是什么底色）
COMPANY_TYPES = [
    "AI原生公司",
    "互联网大厂AI业务线",
    "传统企业数字化转型",
    "AI+垂直行业",
    "外包/咨询",
    "其他",
]

# 第二维度：岗位实质（这个岗位实际干什么）
JOB_NATURES = [
    "核心AI产品",
    "AI应用/集成",
    "数据/平台产品",
    "挂AI标签的传统岗",
    "其他",
]

CONFIDENCE_LEVELS = ["高", "中", "低"]

# 第三维度：岗位方向（10 个枚举，最多 1 主 + 1 副）
JOB_DIRECTIONS = [
    "foundation_model",   # 基础模型训练/微调
    "model_infra",       # 模型服务/推理优化
    "data_platform",     # 数据平台/标注体系
    "agent",             # Agent/智能体
    "rag",               # RAG/知识库
    "c_end_ai",          # C端AI产品
    "b_end_solution",    # B端AI解决方案
    "aigc",              # AI内容生成
    "ai_search_rec",     # AI搜索/推荐
    "ai_vertical",       # AI+垂直行业（direction_detail 写具体行业）
]

JOB_DIRECTION_LABELS = {
    "foundation_model": "基础模型训练/微调",
    "model_infra": "模型服务/推理优化",
    "data_platform": "数据平台/标注体系",
    "agent": "Agent/智能体",
    "rag": "RAG/知识库",
    "c_end_ai": "C端AI产品",
    "b_end_solution": "B端AI解决方案",
    "aigc": "AI内容生成",
    "ai_search_rec": "AI搜索/推荐",
    "ai_vertical": "AI+垂直行业",
}

# 偏好表单用：公司属性 code -> 中文标签（存储用 code）
COMPANY_TYPE_PREF_CODES = [
    ("ai_native", "AI原生公司"),
    ("internet_ai", "互联网大厂AI业务线"),
    ("traditional_digital", "传统企业数字化转型"),
    ("ai_vertical", "AI+垂直行业"),
    ("outsourcing", "外包/咨询"),
    ("other", "其他"),
]

# 偏好表单用：岗位实质 code -> 中文标签（track_preference 存储用 code）
JOB_NATURE_PREF_CODES = [
    ("core_ai_product", "核心AI产品"),
    ("ai_integration", "AI应用/集成"),
    ("data_platform_product", "数据/平台产品"),
    ("traditional_pm", "挂AI标签的传统岗"),
    ("other", "其他"),
]


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
    if all(os.getenv(k) for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")):
        return "deepseek_chat"
    if all(os.getenv(k) for k in ("SUPER_MIND_API_KEY", "SUPER_MIND_BASE_URL", "SUPER_MIND_MODEL")):
        return "supermind"
    raise ValueError("请在 .env 中至少配置 DeepSeek 或 Supermind")


def _extract_json_from_text(text: str) -> Dict:
    if not text or not isinstance(text, str):
        raise ValueError("输入文本为空或格式错误")
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        raise ValueError("未找到有效的 JSON 对象")
    json_str = text[start_idx : end_idx + 1]
    json_str = re.sub(r",\s*}", "}", json_str)
    json_str = re.sub(r",\s*]", "]", json_str)
    return json.loads(json_str)


def _format_job_for_prompt(job: Dict, index: int) -> str:
    """单条岗位格式化为一小节，供 prompt 使用（含公司介绍若有）"""
    name = job.get("job_name", "") or ""
    company = job.get("company_name", "") or ""
    industry = job.get("company_industry", "") or ""
    scale = job.get("company_scale", "") or ""
    tags = job.get("job_tags", "") or ""
    requirements = (job.get("job_requirements", "") or "")[:200]
    desc = (job.get("job_desc", "") or "")[:1200]
    intro = (job.get("company_intro", "") or "").strip()[:500]
    intro_block = f"公司介绍: {intro}\n" if intro else ""
    return f"""--- 岗位 [{index}] ---
岗位名称: {name}
公司名称: {company} | 公司行业: {industry} | 规模: {scale}
{intro_block}职位标签: {tags}
职位要求: {requirements}
职位描述（节选）:
{desc}
"""


def _call_llm_track_batch(
    jobs_batch: List[Dict],
    start_index: int,
    model_id: str,
) -> Tuple[List[Dict], Dict]:
    """
    对一批岗位调用 LLM 进行赛道标注。
    Returns:
        (list of {"company_type", "job_nature", "confidence", "job_direction_primary", "job_direction_secondary", "direction_detail"}, token_info)
    """
    api_key, base_url, model_name = _get_model_config(model_id)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=90)

    jobs_text = "\n".join(
        _format_job_for_prompt(job, start_index + i) for i, job in enumerate(jobs_batch)
    )

    company_list = "、".join(COMPANY_TYPES)
    job_list = "、".join(JOB_NATURES)
    confidence_list = "、".join(CONFIDENCE_LEVELS)
    direction_list = ", ".join(JOB_DIRECTIONS)

    prompt = f"""你是一个职位分类专家。请根据下面每条岗位的「公司名称、公司行业、公司介绍（若有）、职位描述」等信息，判断该岗位的三个维度标签，并给出置信度。若某条没有公司介绍，则主要依据公司名称与行业判断。

【第一维度：公司属性】这家公司是什么底色？只选其一。
可选值：{company_list}
- AI原生公司：主营就是AI产品/服务（如商汤、月之暗面、MiniMax）
- 互联网大厂AI业务线：字节、阿里、腾讯等的AI部门
- 传统企业数字化转型：制造业、金融、地产等在内部搞AI
- AI+垂直行业：教育、医疗、法律、电商等细分场景
- 外包/咨询：帮别人做AI项目，不是自己的产品
- 其他：无法归入以上或信息不足

【第二维度：岗位实质】这个岗位实际干什么？只选其一。
可选值：{job_list}
- 核心AI产品：负责AI能力本身的产品设计
- AI应用/集成：把别人的AI能力集成到业务里
- 数据/平台产品：做数据中台、标注平台、模型管理
- 挂AI标签的传统岗：实际是普通PM/运营，JD里塞了几个AI关键词
- 其他：无法归入以上或信息不足

【第三维度：岗位方向】该岗位主要做哪类 AI 方向？只选一个主方向。
可选值（英文 code）：{direction_list}
- foundation_model：基础模型训练/微调
- model_infra：模型服务/推理优化
- data_platform：数据平台/标注体系
- agent：Agent/智能体
- rag：RAG/知识库
- c_end_ai：C端AI产品
- b_end_solution：B端AI解决方案
- aigc：AI内容生成
- ai_search_rec：AI搜索/推荐
- ai_vertical：AI+垂直行业（direction_detail 只写具体行业如医疗/教育/法律，不写做什么）
job_direction_primary 必填其一；若为 ai_vertical，direction_detail 写具体行业名，否则 direction_detail 可空。

【置信度】高、中、低。信息充分且判断明确选「高」；部分推断选「中」；信息少或模糊选「低」。

【岗位列表】
{jobs_text}

【输出格式】必须只输出严格可解析的 JSON，不要任何其他文字。每条岗位对应一项，按上面 [0]、[1]、[2] 的顺序。
{{"results": [
  {{"index": 0, "company_type": "AI原生公司", "job_nature": "核心AI产品", "confidence": "高", "job_direction_primary": "agent", "direction_detail": ""}},
  {{"index": 1, "company_type": "互联网大厂AI业务线", "job_nature": "AI应用/集成", "confidence": "中", "job_direction_primary": "c_end_ai", "direction_detail": ""}}
]}}
注意：company_type 必须是 [{company_list}] 之一；job_nature 必须是 [{job_list}] 之一；confidence 必须是 [{confidence_list}] 之一；job_direction_primary 必须是 [{direction_list}] 之一。"""

    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "system",
                    "content": "你是职位分类专家。你必须只输出严格可解析的 JSON，不要输出任何解释、前后缀、代码块、markdown 或多余文字。",
                },
                {"role": "user", "content": prompt},
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
            return (
                [
                    {
                        "company_type": "其他",
                        "job_nature": "其他",
                        "confidence": "低",
                        "job_direction_primary": "",
                        "job_direction_secondary": "",
                        "direction_detail": "",
                    }
                    for _ in jobs_batch
                ],
                token_info,
            )

        out = []
        for i in range(len(jobs_batch)):
            orig_idx = start_index + i
            r = next((x for x in results if x.get("index") == orig_idx or x.get("index") == i), None)
            if r is None:
                r = results[i] if i < len(results) else {}
            ct = str(r.get("company_type", "其他")).strip()
            jn = str(r.get("job_nature", "其他")).strip()
            conf = str(r.get("confidence", "低")).strip()
            dp = str(r.get("job_direction_primary", "")).strip()
            dd = str(r.get("direction_detail", "")).strip()[:300]
            if ct not in COMPANY_TYPES:
                ct = "其他"
            if jn not in JOB_NATURES:
                jn = "其他"
            if conf not in CONFIDENCE_LEVELS:
                conf = "中"
            if dp not in JOB_DIRECTIONS:
                dp = ""
            out.append({
                "company_type": ct,
                "job_nature": jn,
                "confidence": conf,
                "job_direction_primary": dp,
                "job_direction_secondary": "",
                "direction_detail": dd,
            })
        return out, token_info
    except Exception as e:
        logger.warning(f"赛道标注批次 LLM 调用失败: {e}，该批次标为「其他/其他/低」")
        return (
            [
                {
                    "company_type": "其他",
                    "job_nature": "其他",
                    "confidence": "低",
                    "job_direction_primary": "",
                    "job_direction_secondary": "",
                    "direction_detail": "",
                }
                for _ in jobs_batch
            ],
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )


def run_track_label_task(
    task_id: str,
    model_id: Optional[str] = None,
    only_unlabeled: bool = True,
    limit: int = 99999,
) -> None:
    """
    在后台线程中执行赛道标注任务：从 DB 取有职位描述的岗位，小批量调 LLM，结果写回 DB。
    """
    from task_manager import task_manager, TaskStatus
    from task_log_handler import TaskLogHandler
    from db import get_jobs_to_label_track, update_job_track_label

    task_handler = TaskLogHandler(task_id=task_id)
    task_handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    task_handler.setFormatter(formatter)
    log = logging.getLogger(__name__)
    log.addHandler(task_handler)
    log.setLevel(logging.INFO)

    try:
        task_manager.update_status(task_id, TaskStatus.RUNNING)
        task_manager.add_log(task_id, "开始赛道标注任务", "INFO")

        model_id = model_id or _get_default_model_id()
        task_manager.add_log(task_id, f"使用模型: {model_id}，处理未标注及遗漏字段的岗位", "INFO")
        task_manager.add_log(task_id, f"每批 {BATCH_SIZE} 条（可设置 TRACK_LABEL_BATCH_SIZE 调整，默认 10）", "INFO")

        jobs = get_jobs_to_label_track(limit=limit, only_unlabeled=only_unlabeled)
        total = len(jobs)
        if total == 0:
            task_manager.add_log(task_id, "没有待标注岗位（所有岗位均已完整标注）", "WARNING")
            task_manager.update_status(task_id, TaskStatus.COMPLETED)
            task_manager.update_result(task_id, success_count=0, failed_count=0, total_labeled=0)
            return

        task_manager.update_progress(task_id, 0, total)
        total_tokens = 0
        total_input_tokens = 0
        total_output_tokens = 0
        success_count = 0
        failed_count = 0

        for start in range(0, total, BATCH_SIZE):
            batch = jobs[start : start + BATCH_SIZE]
            batch_results, token_info = _call_llm_track_batch(batch, start, model_id)
            pt = token_info.get("prompt_tokens", 0) or 0
            ct = token_info.get("completion_tokens", 0) or 0
            tt = token_info.get("total_tokens", 0) or 0
            total_tokens += tt
            total_input_tokens += pt
            total_output_tokens += ct

            for i, job in enumerate(batch):
                job_id = job.get("job_id")
                if not job_id:
                    failed_count += 1
                    continue
                res = batch_results[i] if i < len(batch_results) else {
                    "company_type": "其他", "job_nature": "其他", "confidence": "低",
                    "job_direction_primary": "", "job_direction_secondary": "", "direction_detail": "",
                }
                try:
                    update_job_track_label(
                        job_id,
                        res.get("company_type", "其他"),
                        res.get("job_nature", "其他"),
                        res.get("confidence", "中"),
                        res.get("job_direction_primary", ""),
                        res.get("job_direction_secondary", ""),
                        res.get("direction_detail", ""),
                    )
                    success_count += 1
                except Exception as e:
                    failed_count += 1
                    task_manager.add_log(task_id, f"写入 DB 失败 job_id={job_id}: {e}", "WARNING")

            current = min(start + len(batch), total)
            task_manager.update_progress(task_id, current, total)
            task_manager.add_log(
                task_id,
                f"已标注 {current}/{total} 条，本批 Token: 输入 {pt}, 输出 {ct}, 总 {tt}",
                "INFO",
            )

        task_manager.update_result(
            task_id,
            success_count=success_count,
            failed_count=failed_count,
            total_labeled=success_count,
            total_tokens=total_tokens,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
        )
        task_manager.add_log(
            task_id,
            f"赛道标注完成：成功 {success_count}，失败 {failed_count}；Token 输入 {total_input_tokens}，输出 {total_output_tokens}，总 {total_tokens}",
            "INFO",
        )
        task_manager.update_status(task_id, TaskStatus.COMPLETED)
    except Exception as e:
        task_manager.add_log(task_id, f"赛道标注任务失败: {e}", "ERROR")
        task_manager.set_error(task_id, str(e))
    finally:
        log.removeHandler(task_handler)
