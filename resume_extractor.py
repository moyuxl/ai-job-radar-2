"""
简历解析：从 PDF 提取文本，调用 LLM 输出结构化 profile JSON，保存到本地。
以简历原文为准，不编造；部分字段（如行业、年限）允许按上下文合理推断，见提取 prompt。
"""
import io
import json
import re
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

from pypdf import PdfReader
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
RESUME_DIR = OUTPUT_DIR / "resume"

# 固定模板
PROFILE_SCHEMA = {
    "profile": {
        "years_of_experience": 0,
        "education": "",
        "current_title": "",
        "industry_experience": [],
        "skills_proficient": [],
        "skills_familiar": [],
        "tools": [],
        "highlights": [],
    }
}


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


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """从 PDF 字节流提取文本（适用于可复制文字的 PDF）"""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text_parts = []
    for page in reader.pages:
        t = page.extract_text()
        if t:
            text_parts.append(t)
    text = "\n".join(text_parts).strip()
    if not text or len(text) < 50:
        raise ValueError("PDF 中无法提取到有效文字，请确认是可复制文字的 PDF")
    return text


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


def _normalize_profile(data: Dict) -> Dict:
    """规范化 profile：确保结构符合模板，缺失字段用空值"""
    profile = data.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}
    out = {
        "years_of_experience": int(profile.get("years_of_experience", 0)) if profile.get("years_of_experience") is not None else 0,
        "education": str(profile.get("education", "") or "").strip(),
        "current_title": str(profile.get("current_title", "") or "").strip(),
        "industry_experience": profile.get("industry_experience") if isinstance(profile.get("industry_experience"), list) else [],
        "skills_proficient": profile.get("skills_proficient") if isinstance(profile.get("skills_proficient"), list) else [],
        "skills_familiar": profile.get("skills_familiar") if isinstance(profile.get("skills_familiar"), list) else [],
        "tools": profile.get("tools") if isinstance(profile.get("tools"), list) else [],
        "highlights": profile.get("highlights") if isinstance(profile.get("highlights"), list) else [],
    }
    return {"profile": out}


def extract_profile_from_text(text: str, model_id: str = "") -> Tuple[Dict, Dict]:
    """调用 LLM 从简历文本提取 profile，返回 (profile_dict, token_info)"""
    model_id = model_id or _get_default_model_id()
    api_key, base_url, model_name = _get_model_config(model_id)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=90)

    text = text[:12000]  # 限制长度避免超 token

    prompt = f"""你是一个简历信息提取助手。请根据以下简历文本提取结构化信息。

【总体规则】
- 大部分字段只提取简历中明确写出的信息，不编造、不美化
- 以下两个字段允许从上下文合理推断（见下方说明）

【字段说明】
- years_of_experience: 工作年限，简历未写则 0
- education: 学历（本科、硕士、博士等）
- current_title: 当前/最近职位名称
- industry_experience: 行业经历。**可从项目描述、客户类型推断**：如简历提到「政企项目」「B/G端」「政府」「企业服务」「教育」「医疗」等，应推断出对应行业并填入。格式 [{{"industry": "行业名称", "years": 数字}}, ...]，years 可据项目时长估算
- skills_proficient: 核心/熟练技能（简历重点强调的）
- skills_familiar: **包括简历中提及但非主要技能的技术/工具**，如 Python、SQL、数据分析等若在项目里提到过，即使非核心也应列入
- tools: 使用的工具/软件
- highlights: **简历要点摘要列表，条目要尽量多、信息要覆盖经历与项目，不要只写带数字的业绩。**
  具体要求：
  1) **数量**：建议 **8～20 条**（经历与项目多时可接近 20；若原文较短则如实列全，不凑数编造）。
  2) **内容类型**：除量化成果外，必须包含 **每一段工作经历** 与 **每一个项目/实习** 里**写明了的内容**——例如：公司/团队业务背景、你的职责、参与模块、技术栈、业务场景、交付物、协作方式等；有「负责/参与/主导/搭建/优化」等表述的应落成一条或多条要点。
  3) **不要只写 KPI**：不要仅输出「提升 xx%」「降低 xx」这类句子而忽略「具体做了什么」；若简历既有描述又有数字，可拆成多条或合并为一条完整叙述（背景 + 动作 + 结果）。
  4) **覆盖方式**：按「工作经历」「项目经验」「项目」「实习」等区块**逐段扫描**，**一段经历或一个项目可拆成 1～3 条 highlights**，避免用两三句概括整份简历。
  5) **措辞**：在忠于原文的前提下可简短归纳，但不得添加简历未出现的职责、公司、项目或技术。

【输出格式】必须只输出严格可解析的 JSON，不要任何其他文字。
{{
  "profile": {{
    "years_of_experience": 数字,
    "education": "学历",
    "current_title": "职位名称",
    "industry_experience": [{{"industry": "行业名称", "years": 数字}}, ...],
    "skills_proficient": ["熟练技能1", ...],
    "skills_familiar": ["了解技能1", ...],
    "tools": ["工具1", ...],
    "highlights": ["要点1（经历或项目：职责与背景）", "要点2", "要点3"]
  }}
}}

【简历文本】
{text}
"""

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": "你只输出严格可解析的 JSON，不要输出任何解释、前后缀、代码块、markdown 或多余文字。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    content = response.choices[0].message.content.strip()
    data = _extract_json_from_text(content)
    token_info = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if hasattr(response, "usage") and response.usage:
        token_info["prompt_tokens"] = getattr(response.usage, "prompt_tokens", 0) or 0
        token_info["completion_tokens"] = getattr(response.usage, "completion_tokens", 0) or 0
        token_info["total_tokens"] = getattr(response.usage, "total_tokens", 0) or 0
    return _normalize_profile(data), token_info


def save_resume_json(profile: Dict, output_dir: Optional[Path] = None) -> str:
    """保存 profile JSON 到本地，返回文件路径"""
    output_dir = output_dir or RESUME_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"resume_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
    return str(path)


def save_preferences_to_json(file_path: str, preferences: Dict) -> str:
    """
    将偏好合并到已有简历 JSON 文件，写回同一文件。
    file_path: 简历 JSON 文件路径（如 output/resume/resume_xxx.json）
    preferences: 偏好对象，格式见 PROFILE_SCHEMA
    Returns: 文件路径
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
    data["preferences"] = _normalize_preferences(preferences)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path)


def _normalize_preferences(prefs: Dict) -> Dict:
    """规范化偏好字段"""
    if not isinstance(prefs, dict):
        return {}
    return {
        "target_salary_min": int(prefs.get("target_salary_min", 0)) if prefs.get("target_salary_min") is not None else 0,
        "target_salary_max": int(prefs.get("target_salary_max", 0)) if prefs.get("target_salary_max") is not None else 0,
        "target_cities": prefs.get("target_cities") if isinstance(prefs.get("target_cities"), list) else [],
        "company_type_preference": prefs.get("company_type_preference") if isinstance(prefs.get("company_type_preference"), list) else [],
        "company_type_blacklist": prefs.get("company_type_blacklist") if isinstance(prefs.get("company_type_blacklist"), list) else [],
        "track_preference": prefs.get("track_preference") if isinstance(prefs.get("track_preference"), list) else [],
        "dealbreakers": prefs.get("dealbreakers") if isinstance(prefs.get("dealbreakers"), list) else [],
        "other_notes": str(prefs.get("other_notes", "") or "").strip(),
    }


def process_resume_pdf(pdf_bytes: bytes, model_id: str = "") -> Tuple[Dict, str, Dict]:
    """
    完整流程：PDF 提取文本 → LLM 提取 profile → 保存到本地。
    Returns: (profile_dict, saved_file_path, token_info)
    """
    text = extract_text_from_pdf(pdf_bytes)
    profile, token_info = extract_profile_from_text(text, model_id)
    path = save_resume_json(profile)
    pt = token_info.get("prompt_tokens", 0)
    ct = token_info.get("completion_tokens", 0)
    tt = token_info.get("total_tokens", 0)
    logger.info(f"简历解析完成 | Token 输入 {pt}, 输出 {ct}, 合计 {tt}")
    return profile, path, token_info


def list_resume_files() -> list:
    """列出 output/resume 目录下所有 resume_*.json 文件，按修改时间倒序"""
    if not RESUME_DIR.exists():
        return []
    files = []
    for p in RESUME_DIR.glob("resume_*.json"):
        stat = p.stat()
        files.append({"path": str(p), "name": p.name, "modified": stat.st_mtime})
    files.sort(key=lambda x: x["modified"], reverse=True)
    return files


def load_resume_json(file_path: str) -> Dict:
    """
    从本地 JSON 文件加载简历数据（profile + preferences）。
    Returns: {"profile": {...}, "preferences": {...}, "saved_path": str}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")
    # 安全校验：路径必须在 RESUME_DIR 内
    try:
        path.resolve().relative_to(RESUME_DIR.resolve())
    except ValueError:
        raise ValueError("只能加载 output/resume 目录下的简历文件")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
    return {
        "profile": data.get("profile", {}),
        "preferences": data.get("preferences", {}),
        "saved_path": str(path),
    }
