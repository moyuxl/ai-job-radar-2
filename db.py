"""
数据库封装：使用 SQLite 作为岗位与分析结果的主存储。

- 主库文件：jobs.db（与本文件同目录）
- 表：
  - jobs：岗位列表 + 详情信息
  - analysis_results：LLM 分析结果

集成建议：
- 在应用启动时调用 init_db() 确保表结构存在。
- 在列表爬取完成后，遍历 jobs 列表调用 upsert_job_from_crawler(job_info, crawl_params)。
- 在详情页爬取时，调用 update_job_detail(job_id, job_desc) 回写职位描述。
- 在分析阶段，从 get_jobs_to_analyze() 取待分析岗位，分析后用 save_analysis_result() 回写。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from match_agent import HEAVY_THRESHOLD

DB_PATH = Path(__file__).parent / "jobs.db"

# 共性分析：至少需要 N 条带 gap 的深度匹配；若 ≥HEAVY_THRESHOLD 分不足 N 条，则按 match_score 向下补足（仍须为深度匹配）
COMMONALITY_MIN_JOBS = 5


# ---------- 基础工具 ----------

def get_conn() -> sqlite3.Connection:
    """获取 SQLite 连接（row_factory 设置为 dict-like Row）。"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化数据库（建表 + 索引）。"""
    conn = get_conn()
    cur = conn.cursor()

    # jobs 表：岗位列表 + 详情
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id            TEXT PRIMARY KEY,   -- encryptJobId
            city_code         TEXT,
            city_name         TEXT,
            job_name          TEXT,
            degree_name       TEXT,
            degree_code       TEXT,
            experience        TEXT,
            salary_desc       TEXT,
            company_name      TEXT,
            company_industry  TEXT,
            company_scale     TEXT,
            job_tags          TEXT,
            job_requirements  TEXT,
            job_url           TEXT,
            job_desc          TEXT,
            source_keyword    TEXT,
            source_city_code  TEXT,
            source_degree     TEXT,
            source_experience TEXT,
            source_salary     TEXT,
            first_seen_at     TEXT,
            last_seen_at      TEXT,
            detail_updated_at TEXT,
            analyzed          INTEGER DEFAULT 0
        )
        """
    )

    # analysis_results 表：LLM 分析结果（可选，按需写入）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_results (
            job_id               TEXT PRIMARY KEY,
            work_content         TEXT,
            must_have_skills     TEXT,
            nice_to_have_skills  TEXT,
            signals_deliverables TEXT,
            signals_process      TEXT,
            signals_metrics      TEXT,
            signals_fluff        TEXT,
            evidence_snippets    TEXT,
            completeness_score   REAL,
            actionability_score  REAL,
            total_score          REAL,
            fine_grain_score     REAL,
            thin_jd              INTEGER,
            fluffy               INTEGER,
            needs_manual_review  INTEGER,
            total_tokens         INTEGER,
            input_tokens         INTEGER,
            output_tokens        INTEGER,
            analyzed_at          TEXT
        )
        """
    )

    # 常用索引
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_name)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_city ON jobs(city_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_analyzed ON jobs(analyzed)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_score ON analysis_results(total_score DESC)"
    )

    # match_results 表：岗位与简历的匹配评分结果
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS match_results (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id              TEXT NOT NULL,
            resume_path         TEXT NOT NULL,
            match_score         INTEGER,
            skill_match         INTEGER,
            experience_match    INTEGER,
            growth_potential    INTEGER,
            culture_fit         INTEGER,
            strengths           TEXT,
            gaps                TEXT,
            advice              TEXT,
            created_at          TEXT,
            UNIQUE(job_id, resume_path)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_match_resume ON match_results(resume_path)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_match_score ON match_results(match_score DESC)")

    # match_results：深度匹配分析 JSON、版本、粗分层分数（迁移）
    for col_def in [
        ("gap_analysis_json", "TEXT"),
        ("agent_version", "TEXT"),
        ("coarse_score", "INTEGER"),
        ("applied", "INTEGER"),  # 0 未标记 1 已投 2 不投/叉（用户标记）
    ]:
        try:
            cur.execute(f"ALTER TABLE match_results ADD COLUMN {col_def[0]} {col_def[1]}")
        except sqlite3.OperationalError:
            pass

    # agent_analysis 表：差距分析结果（简历 + 岗位）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_analysis (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id              TEXT NOT NULL,
            resume_path         TEXT NOT NULL,
            years_verdict       TEXT,
            gap_items           TEXT,
            materials           TEXT,
            rewrites            TEXT,
            eval_before         TEXT,
            eval_after          TEXT,
            is_years_hard_injury INTEGER,
            created_at          TEXT,
            UNIQUE(job_id, resume_path)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_resume ON agent_analysis(resume_path)")

    # agent_analysis 表：投递推荐字段
    for col_def in [("recommendation", "TEXT"), ("recommendation_msg", "TEXT")]:
        try:
            cur.execute(f"ALTER TABLE agent_analysis ADD COLUMN {col_def[0]} {col_def[1]}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    # 赛道标注字段 + 公司介绍 + 岗位方向（jobs 表）：若表已存在则追加列
    for col_def in [
        ("company_type", "TEXT"),
        ("job_nature", "TEXT"),
        ("track_confidence", "TEXT"),
        ("track_labeled_at", "TEXT"),
        ("company_intro", "TEXT"),
        ("job_direction_primary", "TEXT"),
        ("job_direction_secondary", "TEXT"),
        ("direction_detail", "TEXT"),
    ]:
        try:
            cur.execute(f"ALTER TABLE jobs ADD COLUMN {col_def[0]} {col_def[1]}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    conn.commit()
    conn.close()


# ---------- 列表阶段：写入 / 更新岗位 ----------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert_job_from_crawler(job_info: Dict, crawl_params: Optional[Dict] = None) -> None:
    """
    将一次列表爬取得到的一条岗位写入 jobs 表。

    job_info：来自 zhipin_crawler 的单条岗位字典（包含岗位名称、公司、链接等）。
    crawl_params：本次爬取的条件（keyword/city/degree/experience/salary），用于记录来源。
    """
    if not job_info:
        return

    encrypt_job_id = job_info.get("encryptJobId") or job_info.get("岗位ID") or job_info.get(
        "_encryptJobId"
    )
    if not encrypt_job_id:
        # 没有唯一 ID，则不写入
        return

    job_id = str(encrypt_job_id)
    now = _now_iso()
    crawl_params = crawl_params or {}

    data = {
        "job_id": job_id,
        "city_code": str(job_info.get("城市代码", "")),
        "city_name": (job_info.get("工作地点") or "").split("-")[0]
        if job_info.get("工作地点")
        else "",
        "job_name": job_info.get("岗位名称", ""),
        "degree_name": job_info.get("学历要求", ""),
        "degree_code": str(job_info.get("学历代码", "")),
        "experience": job_info.get("工作经验", ""),
        "salary_desc": job_info.get("薪资范围", ""),
        "company_name": job_info.get("公司名称", ""),
        "company_industry": job_info.get("公司行业", ""),
        "company_scale": job_info.get("公司规模", ""),
        "job_tags": job_info.get("职位标签", ""),
        "job_requirements": job_info.get("职位要求", ""),
        "job_url": job_info.get("岗位链接", ""),
        "source_keyword": crawl_params.get("keyword", ""),
        "source_city_code": crawl_params.get("city", ""),
        "source_degree": crawl_params.get("degree", ""),
        "source_experience": crawl_params.get("experience", ""),
        "source_salary": crawl_params.get("salary", ""),
    }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO jobs (
            job_id, city_code, city_name, job_name, degree_name, degree_code,
            experience, salary_desc, company_name, company_industry, company_scale,
            job_tags, job_requirements, job_url,
            source_keyword, source_city_code, source_degree, source_experience, source_salary,
            first_seen_at, last_seen_at
        ) VALUES (
            :job_id, :city_code, :city_name, :job_name, :degree_name, :degree_code,
            :experience, :salary_desc, :company_name, :company_industry, :company_scale,
            :job_tags, :job_requirements, :job_url,
            :source_keyword, :source_city_code, :source_degree, :source_experience, :source_salary,
            :first_seen_at, :last_seen_at
        )
        ON CONFLICT(job_id) DO UPDATE SET
            city_code        = excluded.city_code,
            city_name        = excluded.city_name,
            job_name         = excluded.job_name,
            degree_name      = excluded.degree_name,
            degree_code      = excluded.degree_code,
            experience       = excluded.experience,
            salary_desc      = excluded.salary_desc,
            company_name     = excluded.company_name,
            company_industry = excluded.company_industry,
            company_scale    = excluded.company_scale,
            job_tags         = excluded.job_tags,
            job_requirements = excluded.job_requirements,
            job_url          = excluded.job_url,
            source_keyword   = excluded.source_keyword,
            source_city_code = excluded.source_city_code,
            source_degree    = excluded.source_degree,
            source_experience= excluded.source_experience,
            source_salary    = excluded.source_salary,
            last_seen_at     = :last_seen_at
        """,
        {**data, "first_seen_at": now, "last_seen_at": now},
    )
    conn.commit()
    conn.close()


# ---------- 详情阶段：待爬详情 + 回写 ----------

def get_jobs_to_crawl_detail(limit: int = 50) -> List[Dict]:
    """
    从 jobs 表中选出还没有职位描述的岗位，用于详情爬取。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT job_id, job_url, job_name
        FROM jobs
        WHERE (job_desc IS NULL OR job_desc = '')
          AND job_url IS NOT NULL AND job_url != ''
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_job_detail(
    job_id: str,
    job_desc: str,
    company_intro: Optional[str] = None,
) -> None:
    """
    更新某个岗位的职位描述；若提供 company_intro 则一并更新公司介绍。
    """
    conn = get_conn()
    cur = conn.cursor()
    now = _now_iso()
    if company_intro is not None:
        cur.execute(
            """
            UPDATE jobs
            SET job_desc = ?, company_intro = ?, detail_updated_at = ?
            WHERE job_id = ?
            """,
            (job_desc, company_intro or "", now, job_id),
        )
    else:
        cur.execute(
            """
            UPDATE jobs
            SET job_desc = ?, detail_updated_at = ?
            WHERE job_id = ?
            """,
            (job_desc, now, job_id),
        )
    conn.commit()
    conn.close()


def get_job_detail_cache_for_ids(job_ids: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
    """
    批量查询已在库中且已有非空职位描述的岗位，用于详情爬取前跳过重复请求。

    返回: job_id -> {"job_desc": str, "company_intro": str|None}
    仅包含 job_desc 非空的记录。
    """
    if not job_ids:
        return {}
    uniq: List[str] = []
    seen = set()
    for jid in job_ids:
        if not jid:
            continue
        s = str(jid)
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    if not uniq:
        return {}
    conn = get_conn()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(uniq))
    cur.execute(
        f"""
        SELECT job_id, job_desc, company_intro
        FROM jobs
        WHERE job_id IN ({placeholders})
          AND job_desc IS NOT NULL AND TRIM(job_desc) != ''
        """,
        uniq,
    )
    rows = cur.fetchall()
    conn.close()
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for r in rows:
        d = dict(r)
        jid = str(d.get("job_id", ""))
        if jid:
            out[jid] = {
                "job_desc": d.get("job_desc") or "",
                "company_intro": d.get("company_intro"),
            }
    return out


# ---------- 赛道标注：待标注岗位 + 回写 ----------

def get_jobs_to_label_track(
    limit: int = 200,
    only_unlabeled: bool = True,
) -> List[Dict]:
    """
    选出有职位描述、用于赛道标注的岗位。
    only_unlabeled=True 时返回需标注的岗位：
    - 从未标注的（company_type 为空）
    - 或已标注但缺少新字段的（job_direction_primary 为空，如早期标注的岗位）
    """
    conn = get_conn()
    cur = conn.cursor()
    if only_unlabeled:
        cur.execute(
            """
            SELECT job_id, job_name, company_name, company_industry, company_scale,
                   job_tags, job_requirements, job_desc, company_intro
            FROM jobs
            WHERE job_desc IS NOT NULL AND job_desc != ''
              AND (
                (company_type IS NULL OR company_type = '')
                OR (job_direction_primary IS NULL OR job_direction_primary = '')
              )
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    else:
        cur.execute(
            """
            SELECT job_id, job_name, company_name, company_industry, company_scale,
                   job_tags, job_requirements, job_desc, company_intro
            FROM jobs
            WHERE job_desc IS NOT NULL AND job_desc != ''
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_job_track_label(
    job_id: str,
    company_type: str,
    job_nature: str,
    confidence: str,
    job_direction_primary: str = "",
    job_direction_secondary: str = "",
    direction_detail: str = "",
) -> None:
    """更新某岗位的赛道标注（公司属性、岗位实质、置信度、岗位方向）及标注时间。"""
    conn = get_conn()
    cur = conn.cursor()
    now = _now_iso()
    cur.execute(
        """
        UPDATE jobs
        SET company_type = ?, job_nature = ?, track_confidence = ?, track_labeled_at = ?,
            job_direction_primary = ?, job_direction_secondary = ?, direction_detail = ?
        WHERE job_id = ?
        """,
        (
            company_type or "",
            job_nature or "",
            confidence or "",
            now,
            job_direction_primary or "",
            job_direction_secondary or "",
            direction_detail or "",
            job_id,
        ),
    )
    conn.commit()
    conn.close()


def get_source_keywords() -> List[str]:
    """
    获取库中曾用于抓取的搜索关键词（jobs.source_keyword），用于赛道筛选标签与匹配阶段下拉框。
    含未做赛道标注的岗位；忽略大小写去重，保留出现次数更多的写法。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT source_keyword, COUNT(*) as cnt
        FROM jobs
        WHERE source_keyword IS NOT NULL AND TRIM(source_keyword) != ''
        GROUP BY source_keyword
        """
    )
    rows = cur.fetchall()
    conn.close()
    # 按小写去重，保留出现次数最多的写法
    by_lower: dict = {}
    for r in rows:
        if not r[0]:
            continue
        kw = str(r[0]).strip()
        cnt = int(r[1]) if len(r) > 1 else 1
        key = kw.lower()
        if key not in by_lower or by_lower[key][1] < cnt:
            by_lower[key] = (kw, cnt)
    return sorted([v[0] for v in by_lower.values()], key=lambda x: x.lower())


def get_jobs_by_track(
    company_types: Optional[List[str]] = None,
    job_natures: Optional[List[str]] = None,
    job_direction_primaries: Optional[List[str]] = None,
    min_confidence: Optional[str] = None,
    source_keywords: Optional[List[str]] = None,
    limit: int = 99999,
) -> List[Dict]:
    """
    按赛道条件筛选已标注的岗位。
    - company_types: 公司属性列表，空/None 表示不筛
    - job_natures: 岗位实质列表，空/None 表示不筛
    - job_direction_primaries: 主方向列表（如 agent/c_end_ai），空/None 表示不筛
    - min_confidence: 最低置信度 "高"=仅高 "中"=高或中 ""/None=全部
    - source_keywords: 搜索关键词列表（如 AI产品经理），按 source_keyword 筛选
    """
    conn = get_conn()
    cur = conn.cursor()
    conditions = [
        "company_type IS NOT NULL AND company_type != ''",
    ]
    params: list = []
    if source_keywords:
        # 忽略大小写匹配：AI产品经理 与 ai产品经理 均能筛出
        placeholders = ",".join("?" * len(source_keywords))
        conditions.append(f"LOWER(source_keyword) IN ({placeholders})")
        params.extend([k.lower() for k in source_keywords])
    if company_types:
        placeholders = ",".join("?" * len(company_types))
        conditions.append(f"company_type IN ({placeholders})")
        params.extend(company_types)
    if job_natures:
        placeholders = ",".join("?" * len(job_natures))
        conditions.append(f"job_nature IN ({placeholders})")
        params.extend(job_natures)
    if job_direction_primaries:
        placeholders = ",".join("?" * len(job_direction_primaries))
        conditions.append(f"job_direction_primary IN ({placeholders})")
        params.extend(job_direction_primaries)
    if min_confidence == "高":
        conditions.append("track_confidence = ?")
        params.append("高")
    elif min_confidence == "中":
        conditions.append("track_confidence IN (?, ?)")
        params.extend(["高", "中"])
    params.append(limit)
    sql = f"""
        SELECT job_id, job_name, company_name, company_industry, company_scale,
               salary_desc, job_url, job_desc, company_intro, company_type, job_nature, track_confidence,
               job_direction_primary, job_direction_secondary, direction_detail,
               city_name, experience, job_tags, job_requirements, track_labeled_at, source_keyword
        FROM jobs
        WHERE {' AND '.join(conditions)}
        ORDER BY track_labeled_at DESC, last_seen_at DESC
        LIMIT ?
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_job_by_id(job_id: str) -> Optional[Dict]:
    """按 job_id 获取岗位详情（含 job_desc、company_type、city_name，供匹配/重评使用）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT job_id, job_name, company_name, company_industry, company_scale,
               salary_desc, job_url, job_desc, company_intro, experience,
               job_tags, job_requirements, company_type, city_name
        FROM jobs WHERE job_id = ?
        """,
        (job_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- 分析阶段：待分析岗位 + 保存结果 ----------

def get_jobs_to_analyze(limit: int = 50) -> List[Dict]:
    """
    选出还未分析、且有职位描述的岗位。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT job_id, job_name, company_name, job_desc
        FROM jobs
        WHERE analyzed = 0
          AND job_desc IS NOT NULL AND job_desc != ''
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_analysis_result(job_id: str, result: Dict, token_info: Dict) -> None:
    """
    保存 LLM 分析结果到 analysis_results，并把 jobs.analyzed 标记为 1。

    result: 已经过上层逻辑整理后的结构化结果（字段命名可按需调整）
    token_info: {prompt_tokens, completion_tokens, total_tokens}
    """
    conn = get_conn()
    cur = conn.cursor()
    now = _now_iso()

    cur.execute(
        """
        INSERT INTO analysis_results (
            job_id,
            work_content, must_have_skills, nice_to_have_skills,
            signals_deliverables, signals_process, signals_metrics, signals_fluff,
            evidence_snippets,
            completeness_score, actionability_score, total_score, fine_grain_score,
            thin_jd, fluffy, needs_manual_review,
            total_tokens, input_tokens, output_tokens,
            analyzed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            work_content         = excluded.work_content,
            must_have_skills     = excluded.must_have_skills,
            nice_to_have_skills  = excluded.nice_to_have_skills,
            signals_deliverables = excluded.signals_deliverables,
            signals_process      = excluded.signals_process,
            signals_metrics      = excluded.signals_metrics,
            signals_fluff        = excluded.signals_fluff,
            evidence_snippets    = excluded.evidence_snippets,
            completeness_score   = excluded.completeness_score,
            actionability_score  = excluded.actionability_score,
            total_score          = excluded.total_score,
            fine_grain_score     = excluded.fine_grain_score,
            thin_jd              = excluded.thin_jd,
            fluffy               = excluded.fluffy,
            needs_manual_review  = excluded.needs_manual_review,
            total_tokens         = excluded.total_tokens,
            input_tokens         = excluded.input_tokens,
            output_tokens        = excluded.output_tokens,
            analyzed_at          = excluded.analyzed_at
        """,
        (
            job_id,
            result.get("工作内容", ""),
            result.get("必备技能", ""),
            result.get("加分技能", ""),
            result.get("signals_deliverables", ""),
            result.get("signals_process_terms", ""),
            result.get("signals_metrics_terms", ""),
            result.get("signals_fluff_terms", ""),
            result.get("evidence_snippets", ""),
            float(result.get("completeness", 0.0) or 0.0),
            float(result.get("actionability", 0.0) or 0.0),
            float(result.get("total", 0.0) or 0.0),
            float(result.get("细分评分", 0.0) or 0.0),
            int(bool(result.get("thin_jd", False))),
            int(bool(result.get("fluffy", False))),
            int(bool(result.get("needs_manual_review", False))),
            int(token_info.get("total_tokens", 0) or 0),
            int(token_info.get("prompt_tokens", 0) or 0),
            int(token_info.get("completion_tokens", 0) or 0),
            now,
        ),
    )

    # 标记为已分析
    cur.execute("UPDATE jobs SET analyzed = 1 WHERE job_id = ?", (job_id,))

    conn.commit()
    conn.close()


# ---------- 查询 TopN / 导出辅助 ----------

def get_top_jobs(limit: int = 10) -> List[Dict]:
    """
    从 jobs + analysis_results 中取综合评分最高的前 N 条。
    可用于导出或二次分析。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            j.job_id,
            j.job_name,
            j.company_name,
            j.job_url,
            a.total_score,
            a.fine_grain_score
        FROM jobs j
        JOIN analysis_results a ON j.job_id = a.job_id
        ORDER BY a.total_score DESC, a.fine_grain_score DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- 匹配评分 ----------

def save_match_result(
    job_id: str,
    resume_path: str,
    match_score: int,
    dimension_scores: Dict,
    strengths: List[str],
    gaps: List[str],
    advice: str,
    gap_analysis_json: Optional[Dict] = None,
    agent_version: Optional[str] = None,
    coarse_score: Optional[int] = None,
) -> None:
    """
    保存单条匹配评分结果。
    gap_analysis_json：深度 Match Agent 的 submit_match_result 完整 JSON（可空）。
    agent_version：如 coarse_v1、match_agent_v1。
    coarse_score：粗分层分数；深度分析时用于对照最终分。
    """
    conn = get_conn()
    cur = conn.cursor()
    now = _now_iso()
    gap_blob = (
        json.dumps(gap_analysis_json, ensure_ascii=False)
        if gap_analysis_json is not None
        else None
    )
    cur.execute(
        """
        INSERT INTO match_results (
            job_id, resume_path, match_score,
            skill_match, experience_match, growth_potential, culture_fit,
            strengths, gaps, advice, created_at,
            gap_analysis_json, agent_version, coarse_score, applied
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(job_id, resume_path) DO UPDATE SET
            match_score = excluded.match_score,
            skill_match = excluded.skill_match,
            experience_match = excluded.experience_match,
            growth_potential = excluded.growth_potential,
            culture_fit = excluded.culture_fit,
            strengths = excluded.strengths,
            gaps = excluded.gaps,
            advice = excluded.advice,
            created_at = excluded.created_at,
            gap_analysis_json = excluded.gap_analysis_json,
            agent_version = excluded.agent_version,
            coarse_score = excluded.coarse_score,
            applied = match_results.applied
        """,
        (
            job_id,
            resume_path,
            match_score,
            dimension_scores.get("skill_match"),
            dimension_scores.get("experience_match"),
            dimension_scores.get("growth_potential"),
            dimension_scores.get("culture_fit"),
            json.dumps(strengths, ensure_ascii=False) if strengths else "[]",
            json.dumps(gaps, ensure_ascii=False) if gaps else "[]",
            advice or "",
            now,
            gap_blob,
            agent_version or "",
            coarse_score,
        ),
    )
    conn.commit()
    conn.close()


def _normalize_applied_status(raw) -> int:
    """SQLite INTEGER 0/1/2；兼容历史仅 0/1。"""
    if raw is None:
        return 0
    try:
        v = int(raw)
        if v in (0, 1, 2):
            return v
        return 1 if v else 0
    except (TypeError, ValueError):
        return 0


def set_match_applied(job_id: str, resume_path: str, applied_status: int) -> bool:
    """
    标记某简历下某岗位的投递状态：0 未标记 1 已投 2 不投（叉）。
    返回是否更新到至少一行（无匹配行时为 False）。
    """
    st = _normalize_applied_status(applied_status)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE match_results
        SET applied = ?
        WHERE job_id = ? AND resume_path = ?
        """,
        (st, job_id, resume_path),
    )
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n > 0


def get_match_result_row(job_id: str, resume_path: str) -> Optional[Dict]:
    """单条 match_results 行（含 gap_analysis_json 原始字符串）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM match_results WHERE job_id = ? AND resume_path = ?",
        (job_id, resume_path),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("strengths"), str):
        try:
            d["strengths"] = json.loads(d["strengths"]) if d["strengths"] else []
        except Exception:
            d["strengths"] = []
    if isinstance(d.get("gaps"), str):
        try:
            d["gaps"] = json.loads(d["gaps"]) if d["gaps"] else []
        except Exception:
            d["gaps"] = []
    return d


def get_match_deep_scan_stats(resume_path: str, threshold: int = 80, agent_version_deep: str = "match_agent_v1") -> Dict[str, int]:
    """
    统计当前简历下已落库的匹配记录（仅能与 jobs 关联上的行）：
    - below_threshold：match_score < threshold（仅粗评、不跑深度属正常）
    - need_deep_backfill：match_score >= threshold 但仍非深度匹配（缺 gap_analysis 或非 match_agent_v1）
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          SUM(CASE WHEN COALESCE(mr.match_score, -1) < ? THEN 1 ELSE 0 END) AS below_th,
          SUM(CASE WHEN COALESCE(mr.match_score, 0) >= ? AND (
                mr.agent_version IS NULL OR TRIM(COALESCE(mr.agent_version, '')) = ''
                OR mr.agent_version != ?
                OR mr.gap_analysis_json IS NULL
                OR LENGTH(TRIM(COALESCE(mr.gap_analysis_json, ''))) <= 2
            ) THEN 1 ELSE 0 END) AS need_deep
        FROM match_results mr
        INNER JOIN jobs j ON mr.job_id = j.job_id
        WHERE mr.resume_path = ?
        """,
        (threshold, threshold, agent_version_deep, resume_path),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"below_threshold": 0, "need_deep_backfill": 0}
    return {
        "below_threshold": int(row[0] or 0),
        "need_deep_backfill": int(row[1] or 0),
    }


def get_match_rows_needing_deep_backfill(
    resume_path: str,
    threshold: int = 80,
    limit: int = 50,
    agent_version_deep: str = "match_agent_v1",
) -> List[Dict]:
    """
    取 match_score >= threshold 但仍需补跑深度匹配的行（与 jobs 可 JOIN），按分数降序，最多 limit 条。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT mr.*
        FROM match_results mr
        INNER JOIN jobs j ON mr.job_id = j.job_id
        WHERE mr.resume_path = ?
          AND COALESCE(mr.match_score, 0) >= ?
          AND (
            mr.agent_version IS NULL OR TRIM(COALESCE(mr.agent_version, '')) = ''
            OR mr.agent_version != ?
            OR mr.gap_analysis_json IS NULL
            OR LENGTH(TRIM(COALESCE(mr.gap_analysis_json, ''))) <= 2
          )
        ORDER BY COALESCE(mr.match_score, -1) DESC, mr.job_id ASC
        LIMIT ?
        """,
        (resume_path, threshold, agent_version_deep, limit),
    )
    rows = cur.fetchall()
    conn.close()
    out: List[Dict] = []
    for row in rows:
        d = dict(row)
        if isinstance(d.get("strengths"), str):
            try:
                d["strengths"] = json.loads(d["strengths"]) if d["strengths"] else []
            except Exception:
                d["strengths"] = []
        if isinstance(d.get("gaps"), str):
            try:
                d["gaps"] = json.loads(d["gaps"]) if d["gaps"] else []
            except Exception:
                d["gaps"] = []
        out.append(d)
    return out


def _rows_to_commonality_dicts(rows) -> List[Dict]:
    out: List[Dict] = []
    for r in rows:
        out.append(
            {
                "job_id": r[0],
                "match_score": r[1],
                "gap_analysis_json": r[2],
                "job_name": r[3],
                "company_name": r[4],
            }
        )
    return out


def _sql_track_conditions_on_jobs_alias(
    alias: str,
    company_types: Optional[List[str]] = None,
    job_natures: Optional[List[str]] = None,
    job_direction_primaries: Optional[List[str]] = None,
    min_confidence: Optional[str] = None,
    source_keywords: Optional[List[str]] = None,
) -> Tuple[str, List]:
    """
    与 get_jobs_by_track 同一套赛道条件，用于 JOIN jobs 时的 AND 子句（表别名 prefix）。
    返回 (SQL 片段, 占位符参数列表)，不含前导 AND。
    """
    conditions = [
        f"{alias}.company_type IS NOT NULL AND {alias}.company_type != ''",
    ]
    params: list = []
    if source_keywords:
        placeholders = ",".join("?" * len(source_keywords))
        conditions.append(f"LOWER({alias}.source_keyword) IN ({placeholders})")
        params.extend([k.lower() for k in source_keywords])
    if company_types:
        placeholders = ",".join("?" * len(company_types))
        conditions.append(f"{alias}.company_type IN ({placeholders})")
        params.extend(company_types)
    if job_natures:
        placeholders = ",".join("?" * len(job_natures))
        conditions.append(f"{alias}.job_nature IN ({placeholders})")
        params.extend(job_natures)
    if job_direction_primaries:
        placeholders = ",".join("?" * len(job_direction_primaries))
        conditions.append(f"{alias}.job_direction_primary IN ({placeholders})")
        params.extend(job_direction_primaries)
    if min_confidence == "高":
        conditions.append(f"{alias}.track_confidence = ?")
        params.append("高")
    elif min_confidence == "中":
        conditions.append(f"{alias}.track_confidence IN (?, ?)")
        params.extend(["高", "中"])
    return " AND ".join(conditions), params


def get_top_deep_matches_for_commonality(
    resume_path: str,
    limit: int = 10,
    track_params: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """
    取某简历下深度匹配（match_agent_v1）且已有 gap_analysis_json 的记录。

    优先 match_score >= HEAVY_THRESHOLD（默认 80），按分数降序；若高分深度匹配不足
    COMMONALITY_MIN_JOBS 条，则按分数向下用「仍 < HEAVY_THRESHOLD 的深度匹配」补足，
    以便共性分析至少可凑满 COMMONALITY_MIN_JOBS 条（若库中深度匹配总数仍不足则原样返回）。

    track_params: 与匹配任务一致（company_types、job_natures、job_direction_primaries、
    min_confidence、source_keywords）。为 None 时不按赛道过滤（与历史行为一致）；
    传入 dict 时与 get_jobs_by_track 对齐，仅在该批岗位内取深度匹配 Top。

    最终最多 limit 条，同分按 job_id 升序。不解析 JSON，原样返回 gap_analysis_json 字符串。
    """
    extra_track = ""
    track_args: List = []
    if track_params is not None:
        extra_track, track_args = _sql_track_conditions_on_jobs_alias(
            "j",
            company_types=track_params.get("company_types") or None,
            job_natures=track_params.get("job_natures") or None,
            job_direction_primaries=track_params.get("job_direction_primaries") or None,
            min_confidence=track_params.get("min_confidence") or None,
            source_keywords=track_params.get("source_keywords") or None,
        )
        extra_track = " AND " + extra_track

    _TMPL = """
        SELECT mr.job_id, mr.match_score, mr.gap_analysis_json,
               j.job_name, j.company_name
        FROM match_results mr
        JOIN jobs j ON mr.job_id = j.job_id
        WHERE mr.resume_path = ?
          AND mr.agent_version = 'match_agent_v1'
          AND mr.gap_analysis_json IS NOT NULL
          AND LENGTH(TRIM(mr.gap_analysis_json)) > 2
          {extra_track}
          AND COALESCE(mr.match_score, -1) {{score_cmp}} ?
        ORDER BY COALESCE(mr.match_score, -1) DESC, mr.job_id ASC
        LIMIT ?
    """
    _BASE = _TMPL.format(extra_track=extra_track)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        _BASE.format(score_cmp=">="),
        (resume_path, *track_args, HEAVY_THRESHOLD, limit),
    )
    high = _rows_to_commonality_dicts(cur.fetchall())

    if len(high) >= COMMONALITY_MIN_JOBS:
        conn.close()
        return high[:limit]

    # 高分不足 COMMONALITY_MIN_JOBS 条：向下补足（多取一些候选以便凑满 limit）
    low_cap = max(50, limit * 5, COMMONALITY_MIN_JOBS * 5)
    cur.execute(
        _BASE.format(score_cmp="<"),
        (resume_path, *track_args, HEAVY_THRESHOLD, low_cap),
    )
    low = _rows_to_commonality_dicts(cur.fetchall())
    conn.close()

    seen = {r["job_id"] for r in high}
    merged: List[Dict] = list(high)
    for r in low:
        if len(merged) >= limit:
            break
        jid = r["job_id"]
        if jid not in seen:
            merged.append(r)
            seen.add(jid)
    return merged


def get_matched_job_ids(resume_path: str) -> set:
    """获取某份简历已匹配过的 job_id 集合，用于跳过重复评分"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT job_id FROM match_results WHERE resume_path = ?",
        (resume_path,),
    )
    rows = cur.fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_match_results_by_resume(resume_path: str, limit: int = 500) -> List[Dict]:
    """按 resume_path 获取匹配结果，按 match_score 降序；含 has_agent_analysis 标记"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT mr.*, j.job_name, j.company_name, j.salary_desc, j.city_name, j.job_url, j.company_type,
               j.job_nature, j.track_confidence, j.source_keyword,
               j.job_direction_primary, j.direction_detail,
               CASE WHEN aa.id IS NOT NULL THEN 1 ELSE 0 END as has_agent_analysis
        FROM match_results mr
        JOIN jobs j ON mr.job_id = j.job_id
        LEFT JOIN agent_analysis aa ON aa.job_id = mr.job_id AND aa.resume_path = mr.resume_path
        WHERE mr.resume_path = ?
        ORDER BY mr.match_score DESC NULLS LAST
        LIMIT ?
        """,
        (resume_path, limit),
    )
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("strengths"), str):
            try:
                d["strengths"] = json.loads(d["strengths"]) if d["strengths"] else []
            except Exception:
                d["strengths"] = []
        if isinstance(d.get("gaps"), str):
            try:
                d["gaps"] = json.loads(d["gaps"]) if d["gaps"] else []
            except Exception:
                d["gaps"] = []
        d["has_agent_analysis"] = bool(d.get("has_agent_analysis"))
        d["applied"] = _normalize_applied_status(d.get("applied"))
        # 深度匹配 JSON（match_agent 产出）
        gap_raw = d.get("gap_analysis_json")
        if gap_raw and isinstance(gap_raw, str):
            try:
                parsed = json.loads(gap_raw) if gap_raw.strip() else None
                d["gap_analysis"] = parsed
                if isinstance(parsed, dict):
                    d["match_recommendation"] = parsed.get("recommendation") or ""
            except Exception:
                d["gap_analysis"] = None
                d["match_recommendation"] = ""
        else:
            d["gap_analysis"] = None
            d["match_recommendation"] = ""
        out.append(d)
    return out


def save_agent_analysis(
    job_id: str,
    resume_path: str,
    years_verdict: Dict,
    gap_items: List[Dict],
    materials: List[Dict],
    rewrites: Optional[List[Dict]] = None,
    eval_before: Optional[Dict] = None,
    eval_after: Optional[Dict] = None,
    is_years_hard_injury: bool = False,
    recommendation: str = "",
    recommendation_msg: str = "",
) -> None:
    """保存差距分析结果"""
    conn = get_conn()
    cur = conn.cursor()
    now = _now_iso()
    cur.execute(
        """
        INSERT INTO agent_analysis (
            job_id, resume_path, years_verdict, gap_items, materials,
            rewrites, eval_before, eval_after, is_years_hard_injury,
            recommendation, recommendation_msg, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, resume_path) DO UPDATE SET
            years_verdict = excluded.years_verdict,
            gap_items = excluded.gap_items,
            materials = excluded.materials,
            rewrites = excluded.rewrites,
            eval_before = excluded.eval_before,
            eval_after = excluded.eval_after,
            is_years_hard_injury = excluded.is_years_hard_injury,
            recommendation = excluded.recommendation,
            recommendation_msg = excluded.recommendation_msg,
            created_at = excluded.created_at
        """,
        (
            job_id,
            resume_path,
            json.dumps(years_verdict, ensure_ascii=False),
            json.dumps(gap_items, ensure_ascii=False),
            json.dumps(materials, ensure_ascii=False),
            json.dumps(rewrites, ensure_ascii=False) if rewrites else "[]",
            json.dumps(eval_before, ensure_ascii=False) if eval_before else None,
            json.dumps(eval_after, ensure_ascii=False) if eval_after else None,
            1 if is_years_hard_injury else 0,
            recommendation or "",
            recommendation_msg or "",
            now,
        ),
    )
    conn.commit()
    conn.close()


def get_agent_analysis(job_id: str, resume_path: str) -> Optional[Dict]:
    """获取某简历+岗位的差距分析结果"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM agent_analysis WHERE job_id = ? AND resume_path = ?",
        (job_id, resume_path),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    for k in ("years_verdict", "gap_items", "materials", "rewrites", "eval_before", "eval_after"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k]) if d[k] else None
            except Exception:
                pass
    d["is_years_hard_injury"] = bool(d.get("is_years_hard_injury"))
    # 旧数据兼容：若无 recommendation 则根据 eval_after 计算
    if not d.get("recommendation") and d.get("eval_after"):
        ev = d["eval_after"] or {}
        scores = [ev.get("skill_match"), ev.get("experience_match"), ev.get("growth_potential"), ev.get("culture_fit")]
        valid = [x for x in scores if x is not None and x != ""]
        total = round(sum(valid) / len(valid)) if valid else 0
        if total < 70:
            d["recommendation"] = "不建议投递"
            d["recommendation_msg"] = "该岗位不建议投递，核心差距不可通过简历改写弥补"
        elif total < 75:
            d["recommendation"] = "可以试但概率低"
            d["recommendation_msg"] = ""
        elif total < 80:
            d["recommendation"] = "谨慎投递"
            d["recommendation_msg"] = ""
        else:
            d["recommendation"] = "投递"
            d["recommendation_msg"] = ""
    return d


__all__ = [
    "init_db",
    "upsert_job_from_crawler",
    "get_jobs_to_crawl_detail",
    "update_job_detail",
    "get_jobs_to_label_track",
    "update_job_track_label",
    "get_source_keywords",
    "get_jobs_by_track",
    "get_jobs_to_analyze",
    "save_analysis_result",
    "get_top_jobs",
    "get_conn",
    "save_match_result",
    "get_match_results_by_resume",
    "get_match_deep_scan_stats",
    "get_match_rows_needing_deep_backfill",
    "set_match_applied",
    "get_top_deep_matches_for_commonality",
    "COMMONALITY_MIN_JOBS",
    "get_matched_job_ids",
    "get_match_result_row",
    "get_job_by_id",
    "get_job_detail_cache_for_ids",
    "save_agent_analysis",
    "get_agent_analysis",
]

