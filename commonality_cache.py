"""
共性报告本地缓存：统一放在 output/commonality/，文件名 {简历主文件名}.commonality_report.json。
仅允许 RESUME_DIR 下的简历路径（与 load_resume_json 一致）。
"""
from __future__ import annotations

import hashlib
import json
from json import JSONDecodeError
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from resume_extractor import OUTPUT_DIR, RESUME_DIR

logger = logging.getLogger(__name__)

# 与简历同目录的旧版缓存路径（仅读取时兼容，新写入一律用 COMMONALITY_CACHE_DIR）
COMMONALITY_CACHE_DIR = OUTPUT_DIR / "commonality"


def _resolved_resume_in_resume_dir(resume_path: str) -> Path:
    path = Path(resume_path).resolve()
    path.relative_to(RESUME_DIR.resolve())
    return path


def _track_params_suffix(track_params: Optional[Dict[str, Any]]) -> str:
    """
    无 track_params（None）时使用历史单文件命名；
    有 track_params 时按规范化 JSON 哈希区分不同赛道/关键词组合。
    """
    if track_params is None:
        return ""
    key = json.dumps(track_params, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f".tp_{h}"


def commonality_cache_path(
    resume_path: str, track_params: Optional[Dict[str, Any]] = None
) -> Path:
    """新路径：output/commonality/{stem}.commonality_report[.tp_{hash}].json"""
    p = _resolved_resume_in_resume_dir(resume_path)
    suf = _track_params_suffix(track_params)
    return COMMONALITY_CACHE_DIR / f"{p.stem}.commonality_report{suf}.json"


def _legacy_commonality_cache_path(resume_path: str) -> Path:
    """旧路径：output/resume/{stem}.commonality_report.json（与简历同目录）"""
    p = _resolved_resume_in_resume_dir(resume_path)
    return p.parent / f"{p.stem}.commonality_report.json"


def _read_cache_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        return data
    except JSONDecodeError as e:
        logger.warning("共性报告缓存 JSON 损坏: %s", e)
        return None
    except OSError as e:
        logger.warning("读取共性报告缓存失败: %s", e)
        return None


def read_commonality_cache(
    resume_path: str, track_params: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    读取本地缓存。
    - track_params 为 None：先读无后缀文件，再读旧路径（与简历同目录）。
    - track_params 已提供：只读对应 .tp_ 哈希文件，不回退到无赛道区分的旧缓存，避免与当前筛选不一致。
    """
    primary = commonality_cache_path(resume_path, track_params=track_params)
    legacy = _legacy_commonality_cache_path(resume_path)
    if track_params is None:
        paths = [primary, legacy]
    else:
        paths = [primary]
    seen = set()
    for path in paths:
        rp = str(path.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        data = _read_cache_file(path)
        if data is not None:
            return data
    return None


def write_commonality_cache(
    resume_path: str,
    payload: Dict[str, Any],
    track_params: Optional[Dict[str, Any]] = None,
) -> None:
    """写入 output/commonality/（原子替换）。payload 建议与 POST /api/match/commonality_report 成功响应一致。"""
    cf = commonality_cache_path(resume_path, track_params=track_params)
    COMMONALITY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cf.with_name(cf.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(cf)
    logger.info("共性报告已写入本地: %s", cf)
