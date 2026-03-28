"""
Web 操作台：提供爬虫任务的 Web 界面和 API
"""
import json
import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import logging
import pandas as pd

from task_manager import task_manager, TaskStatus
from crawler_service import start_crawl_task
from analysis_service import start_analysis_task
from track_label_service import start_track_label_task as start_track_label_task_service
from api_server import get_available_models
from time_estimator import get_estimated_end_time, format_estimated_range

# 导入代码映射
try:
    from city_codes import COMMON_CITIES
    from degree_codes import COMMON_DEGREES
    from experience_codes import COMMON_EXPERIENCES
    from salary_codes import COMMON_SALARIES
    HAS_CODE_MAPS = True
except ImportError as e:
    HAS_CODE_MAPS = False
    logger.warning(f"代码映射未找到: {e}")
    COMMON_CITIES = [('100010000', '全国')]
    COMMON_DEGREES = [('0', '不限')]
    COMMON_EXPERIENCES = [('101', '不限经验')]
    COMMON_SALARIES = [('', '不限薪资')]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 启动时初始化本地数据库（若存在 db 模块）
try:
    from db import init_db, get_jobs_by_track, get_source_keywords, get_match_results_by_resume, get_agent_analysis, set_match_applied
    init_db()
    logger.info("本地数据库 jobs.db 已初始化")
except ImportError:
    get_jobs_by_track = None
    get_source_keywords = None
    get_match_results_by_resume = None
    get_agent_analysis = None
    set_match_applied = None

try:
    from track_labeler import (
        COMPANY_TYPES, JOB_NATURES, CONFIDENCE_LEVELS, JOB_DIRECTIONS, JOB_DIRECTION_LABELS,
        COMPANY_TYPE_PREF_CODES, JOB_NATURE_PREF_CODES,
    )
except ImportError:
    COMPANY_TYPES = []
    JOB_NATURES = []
    CONFIDENCE_LEVELS = []
    JOB_DIRECTIONS = []
    JOB_DIRECTION_LABELS = {}
    COMPANY_TYPE_PREF_CODES = []
    JOB_NATURE_PREF_CODES = []

app = FastAPI(title="AI 职位雷达 - Web 操作台", version="1.0.0")

# 静态文件与模板目录
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
templates_dir = Path(__file__).parent / "templates"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


class CrawlRequest(BaseModel):
    """爬虫请求模型"""
    keyword: str
    city: str = "100010000"
    degree: str = ""
    experience: str = ""  # 空=不限（显示全部），101=经验不限
    salary: str = ""
    max_pages: int = 1
    crawl_details: bool = True
    enable_llm_filter: bool = False
    filter_model_id: str = ""


class AnalysisRequest(BaseModel):
    """分析请求模型"""
    excel_path: str  # 原始数据 Excel 文件路径
    model_id: str = ""  # 模型 ID（supermind/deepseek），空时使用默认


class TrackLabelRequest(BaseModel):
    """赛道标注请求模型"""
    model_id: str = ""  # 模型 ID，空时使用默认（DeepSeek Chat）
    only_unlabeled: bool = True  # 仅标注未标注的岗位


class TrackFilterRequest(BaseModel):
    """赛道筛选请求模型"""
    company_types: List[str] = []  # 公司属性，空为不筛
    job_natures: List[str] = []    # 岗位实质，空为不筛
    job_direction_primaries: List[str] = []  # 主方向，空为不筛
    min_confidence: str = ""      # 最低置信度：高/中/空(全部)
    source_keywords: List[str] = []  # 搜索关键词（如 AI产品经理），按 source_keyword 筛选


class PreferenceSaveRequest(BaseModel):
    """求职偏好保存请求"""
    saved_path: str  # 简历 JSON 文件路径
    preferences: dict  # 偏好对象


class ResumeLoadRequest(BaseModel):
    """加载简历文件请求"""
    path: str  # 简历 JSON 文件路径


class MatchStartRequest(BaseModel):
    """匹配任务启动请求"""
    resume_path: str
    track_params: dict  # company_types, job_natures, job_direction_primaries, min_confidence, source_keywords
    hard_filter_overrides: dict = {}  # target_salary_min, target_salary_max, target_cities, company_type_blacklist
    model_id: str = ""


class MatchRerunOneRequest(BaseModel):
    """单岗位重新匹配（粗评 + 深度 Match Agent，覆盖原结果）"""
    job_id: str
    resume_path: str
    model_id: str = "deepseek_chat"


class MatchAppliedRequest(BaseModel):
    """标记该简历下某岗位投递状态（本地 match_results.applied：0 未标记 1 已投 2 不投）"""
    job_id: str
    resume_path: str
    # 新：优先使用 applied_status；旧客户端仍传 applied true/false
    applied: Optional[bool] = None
    applied_status: Optional[int] = None


class GapStartRequest(BaseModel):
    """差距分析任务启动请求"""
    job_id: str
    resume_path: str
    model_id: str = "deepseek_chat"


class CommonalityReportRequest(BaseModel):
    """头部深度匹配岗位共性分析（仅前端展示，不落库）"""
    resume_path: str
    model_id: str = "deepseek_chat"
    top_n: int = 10
    # 与「开始匹配」一致：仅在该赛道/关键词批次内取深度匹配 TopN；不传则与历史行为一致（不按赛道过滤）
    track_params: Optional[Dict[str, Any]] = None


class MasterRewriteRequest(BaseModel):
    """基于共性报告的主简历改写（一次改写，覆盖头部岗位共性）"""
    resume_path: str
    model_id: str = "deepseek_chat"
    commonality_report: dict


class TaskStatusResponse(BaseModel):
    """任务状态响应模型"""
    task_id: str
    status: str
    progress: dict
    logs: list
    result: dict
    error: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration: float = 0.0
    waiting_message: Optional[str] = None  # 等待确认时的提示消息
    estimated_end_time: Optional[str] = None  # 预计完成时间 "HH:MM"
    estimated_range: Optional[str] = None  # 显示用 "10:00 开始，预计 10:30 完成"


@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...), model_id: str = Form("")):
    """上传 PDF 简历，提取文本后调用 LLM 输出 profile JSON，保存到本地"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="请上传 PDF 文件")
    try:
        from resume_extractor import process_resume_pdf
        pdf_bytes = await file.read()
        if len(pdf_bytes) < 100:
            raise HTTPException(status_code=400, detail="文件过小或为空")
        profile, saved_path, token_info = process_resume_pdf(pdf_bytes, model_id=model_id or "")
        return {
            "profile": profile,
            "saved_path": saved_path,
            "token_info": token_info,
            "message": f"简历已解析并保存至 {saved_path}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"简历解析失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/resume/list")
async def list_resume_files_api():
    """列出已保存的简历 JSON 文件"""
    try:
        from resume_extractor import list_resume_files
        files = list_resume_files()
        return {"files": files}
    except Exception as e:
        logger.error(f"列出简历文件失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/resume/load")
async def load_resume_file(request: ResumeLoadRequest):
    """加载简历 JSON 文件内容（profile + preferences）"""
    try:
        from resume_extractor import load_resume_json
        data = load_resume_json(request.path)
        return data
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"加载简历文件失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/resume/preferences")
async def save_resume_preferences(request: PreferenceSaveRequest):
    """将求职偏好合并到已有简历 JSON 文件"""
    try:
        from resume_extractor import save_preferences_to_json
        path = save_preferences_to_json(request.saved_path, request.preferences)
        return {"saved_path": path, "message": "偏好已保存并合并到简历文件"}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"保存偏好失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/match/start")
async def start_match_task(request: MatchStartRequest):
    """启动匹配评分任务"""
    try:
        from match_service import run_match_task
        from task_manager import task_manager
        import threading
        task_id = task_manager.create_task("match", {
            "resume_path": request.resume_path,
            "track_params": request.track_params,
            "hard_filter_overrides": request.hard_filter_overrides,
            "model_id": request.model_id or "",
        })
        thread = threading.Thread(
            target=run_match_task,
            args=(task_id, request.resume_path, request.track_params, request.hard_filter_overrides, request.model_id or ""),
        )
        thread.daemon = True
        thread.start()
        return {"task_id": task_id, "message": "匹配任务已启动"}
    except Exception as e:
        logger.error(f"启动匹配任务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/match/rerun_one")
async def match_rerun_one(request: MatchRerunOneRequest):
    """对单个岗位重新执行粗评 + 深度 Match Agent（覆盖 match_results）"""
    try:
        from match_service import run_rerun_match_one
        from task_manager import task_manager
        import threading

        task_id = task_manager.create_task("match_rerun", {
            "job_id": request.job_id,
            "resume_path": request.resume_path,
            "model_id": request.model_id or "",
        })
        thread = threading.Thread(
            target=run_rerun_match_one,
            args=(task_id, request.job_id, request.resume_path, request.model_id or "deepseek_chat"),
        )
        thread.daemon = True
        thread.start()
        return {"task_id": task_id, "message": "单岗位重新匹配已启动"}
    except Exception as e:
        logger.error(f"启动单岗位重新匹配失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/match/results")
async def get_match_results(resume_path: str):
    """获取指定简历的匹配结果列表"""
    if get_match_results_by_resume is None:
        raise HTTPException(status_code=503, detail="数据库模块不可用")
    try:
        results = get_match_results_by_resume(resume_path)
        return {"results": results}
    except Exception as e:
        logger.error(f"获取匹配结果失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _resolve_match_applied_status(req: MatchAppliedRequest) -> int:
    if req.applied_status is not None and req.applied_status in (0, 1, 2):
        return int(req.applied_status)
    if req.applied is not None:
        return 1 if req.applied else 0
    return 0


@app.post("/api/match/applied")
async def match_set_applied(request: MatchAppliedRequest):
    """更新匹配结果投递标记（0/1/2，见 MatchAppliedRequest）"""
    if set_match_applied is None:
        raise HTTPException(status_code=503, detail="数据库模块不可用")
    try:
        st = _resolve_match_applied_status(request)
        ok = set_match_applied(request.job_id, request.resume_path, st)
        if not ok:
            raise HTTPException(status_code=404, detail="无对应匹配记录，请先完成匹配")
        return {"ok": True, "applied_status": st}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新已投递状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/match/commonality_report/cached")
async def get_commonality_report_cached(
    resume_path: str,
    track_params: Optional[str] = Query(
        None,
        description="JSON 字符串，与 POST 共性分析 body 中 track_params 一致（赛道/关键词）",
    ),
):
    """
    读取本地缓存的共性报告（output/commonality/{stem}.commonality_report[.tp_哈希].json；兼容旧版简历同目录）。
    无缓存或无效时 404。
    """
    try:
        from commonality_cache import read_commonality_cache

        tp: Optional[Dict[str, Any]] = None
        if track_params and track_params.strip():
            try:
                parsed = json.loads(track_params)
                tp = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="track_params 不是合法 JSON")

        data = read_commonality_cache(resume_path, track_params=tp)
        if not data:
            raise HTTPException(status_code=404, detail="无本地共性报告缓存")
        return {**data, "cached": True}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("读取共性报告缓存失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/match/commonality_report")
async def match_commonality_report(request: CommonalityReportRequest):
    """
    对当前简历下深度匹配（match_agent_v1）得分最高的至多 top_n 条做共性分析。
    若提供 track_params，与「开始匹配」赛道条件一致，仅在该批岗位内取 TopN。
    输入为截断后的 gap 切片 + profile，不落库；成功后写入本地 JSON 缓存。
    """
    try:
        from commonality_analysis import run_commonality_analysis
        from commonality_cache import write_commonality_cache

        top_n = request.top_n
        if top_n < 1:
            top_n = 1
        if top_n > 20:
            top_n = 20
        tp = request.track_params
        logger.info(
            "[API 共性分析] 请求 | resume_path=%s | model_id=%s | top_n=%s | track_params=%s",
            request.resume_path,
            request.model_id or "deepseek_chat",
            top_n,
            "set" if tp is not None else "none",
        )
        result, token_info = run_commonality_analysis(
            request.resume_path,
            model_id=request.model_id or "deepseek_chat",
            top_n=top_n,
            track_params=tp,
        )
        if not result.get("ok"):
            logger.warning(
                "[API 共性分析] 业务失败 | resume_path=%s | detail=%s",
                request.resume_path,
                result.get("error"),
            )
            raise HTTPException(status_code=400, detail=result.get("error") or "共性分析不可用")
        logger.info(
            "[API 共性分析] 成功 | resume_path=%s | job_count=%s | token in/out/total=%s/%s/%s",
            request.resume_path,
            result.get("job_count"),
            token_info.get("prompt_tokens"),
            token_info.get("completion_tokens"),
            token_info.get("total_tokens"),
        )
        payload = {**result, "token_info": token_info}
        try:
            write_commonality_cache(request.resume_path, payload, track_params=tp)
        except Exception as e:
            logger.warning("共性分析成功但写入本地缓存失败: %s", e)
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("共性分析失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/gap/start")
async def start_gap_task(request: GapStartRequest):
    """启动差距分析任务（单岗模式，已弃用主流程；保留接口兼容）"""
    try:
        from gap_service import run_gap_task
        from task_manager import task_manager
        import threading
        task_id = task_manager.create_task("gap", {
            "job_id": request.job_id,
            "resume_path": request.resume_path,
        })
        thread = threading.Thread(
            target=run_gap_task,
            args=(task_id, request.job_id, request.resume_path, request.model_id or "deepseek_chat"),
        )
        thread.daemon = True
        thread.start()
        return {"task_id": task_id, "message": "差距分析任务已启动"}
    except Exception as e:
        logger.error(f"启动差距分析失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/gap/master_rewrite")
async def start_master_rewrite_task(request: MasterRewriteRequest):
    """基于共性报告的主简历改写（一次任务，结果存 agent_analysis，job_id=__COMMONALITY_MASTER__）"""
    try:
        from gap_service import run_master_rewrite_task
        from task_manager import task_manager
        import threading

        if not request.commonality_report:
            raise HTTPException(status_code=400, detail="请先提供共性报告 commonality_report")

        task_id = task_manager.create_task("gap_master", {
            "resume_path": request.resume_path,
        })
        thread = threading.Thread(
            target=run_master_rewrite_task,
            args=(
                task_id,
                request.resume_path,
                request.model_id or "deepseek_chat",
                request.commonality_report,
            ),
        )
        thread.daemon = True
        thread.start()
        return {"task_id": task_id, "message": "主简历改写任务已启动"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启动主简历改写失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gap/result")
async def get_gap_result(job_id: str, resume_path: str):
    """获取差距分析结果"""
    if get_agent_analysis is None:
        raise HTTPException(status_code=503, detail="数据库模块不可用")
    try:
        result = get_agent_analysis(job_id, resume_path)
        return {"result": result}
    except Exception as e:
        logger.error(f"获取差距分析失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gap/master_result")
async def get_master_rewrite_result(resume_path: str):
    """获取共性驱动主简历改写结果（与单岗 gap/result 区分）"""
    if get_agent_analysis is None:
        raise HTTPException(status_code=503, detail="数据库模块不可用")
    try:
        from gap_agent import COMMONALITY_MASTER_JOB_ID

        result = get_agent_analysis(COMMONALITY_MASTER_JOB_ID, resume_path)
        return {"result": result}
    except Exception as e:
        logger.error(f"获取主简历改写结果失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=HTMLResponse)
async def landing():
    """Landing Page（营销首页）"""
    html_file = templates_dir / "landing.html"
    return HTMLResponse(content=html_file.read_text(encoding="utf-8"))


@app.get("/workbench", response_class=HTMLResponse)
async def workbench():
    """Web 操作台 / 工作台（原首页功能）"""
    html_file = templates_dir / "web_console.html"
    return HTMLResponse(content=html_file.read_text(encoding="utf-8"))


@app.post("/api/crawl/start")
async def start_crawl(request: CrawlRequest):
    """启动爬虫任务"""
    try:
        params = {
            "keyword": request.keyword,
            "city": request.city,
            "degree": request.degree,
            "experience": request.experience,
            "salary": request.salary,
            "max_pages": request.max_pages,
            "crawl_details": request.crawl_details,
            "enable_llm_filter": request.enable_llm_filter,
            "filter_model_id": request.filter_model_id or ""
        }
        
        task_id = start_crawl_task(params, output_dir="output")
        
        return {"task_id": task_id, "message": "任务已启动"}
    except Exception as e:
        logger.error(f"启动任务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/task/{task_id}/status", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """获取任务状态"""
    task = task_manager.get_task(task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    # 获取最近日志
    recent_logs = task_manager.get_recent_logs(task_id, limit=20)
    
    # 计算预计完成时间（仅运行中任务显示）
    estimated_end_time = None
    estimated_range = None
    if task["status"] in ("running", "waiting_confirm"):
        estimated_end_time = get_estimated_end_time(
            task.get("start_time"),
            task.get("task_type", "crawl"),
            task.get("params", {}),
            task.get("progress", {}),
            task.get("result", {}),
        )
        if estimated_end_time:
            estimated_range = format_estimated_range(task.get("start_time"), estimated_end_time)
    
    return TaskStatusResponse(
        task_id=task["task_id"],
        status=task["status"],
        progress=task["progress"],
        logs=recent_logs,
        result=task["result"],
        error=task.get("error"),
        start_time=task.get("start_time"),
        end_time=task.get("end_time"),
        duration=task.get("duration", 0.0),
        waiting_message=task.get("waiting_message"),
        estimated_end_time=estimated_end_time,
        estimated_range=estimated_range,
    )


@app.post("/api/task/{task_id}/confirm")
async def confirm_task(task_id: str):
    """确认任务继续执行（如登录确认）"""
    success = task_manager.confirm_task(task_id)
    
    if not success:
        raise HTTPException(status_code=400, detail="任务不存在或不在等待确认状态")
    
    return {"message": "确认成功", "task_id": task_id}


@app.get("/api/options/cities")
async def get_city_options():
    """获取城市选项列表"""
    return {"options": [{"code": code, "name": name} for code, name in COMMON_CITIES]}


@app.get("/api/options/degrees")
async def get_degree_options():
    """获取学历选项列表"""
    return {"options": [{"code": code, "name": name} for code, name in COMMON_DEGREES]}


@app.get("/api/options/experiences")
async def get_experience_options():
    """获取工作经验选项列表"""
    return {"options": [{"code": code, "name": name} for code, name in COMMON_EXPERIENCES]}


@app.get("/api/options/salaries")
async def get_salary_options():
    """获取薪资选项列表"""
    return {"options": [{"code": code, "name": name} for code, name in COMMON_SALARIES]}


@app.get("/api/options/models")
async def get_model_options():
    """获取分析模型选项列表（从 .env 已配置的模型）"""
    models = get_available_models()
    return {"options": [{"id": m["id"], "name": m["name"]} for m in models]}


@app.get("/api/file/{file_path:path}")
async def download_file(file_path: str):
    """下载结果文件"""
    import urllib.parse
    file_path = urllib.parse.unquote(file_path)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    
    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=os.path.basename(file_path)
    )


@app.post("/api/analysis/start")
async def start_analysis(request: AnalysisRequest):
    """启动分析任务"""
    try:
        # 验证文件是否存在
        excel_path = Path(request.excel_path)
        if not excel_path.exists():
            raise HTTPException(status_code=400, detail=f"文件不存在: {request.excel_path}")
        
        task_id = start_analysis_task(
            str(excel_path.absolute()),
            output_dir="output",
            model_id=request.model_id or ""
        )
        
        return {"task_id": task_id, "message": "分析任务已启动"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启动分析任务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/options/company-types")
async def get_company_type_options():
    """赛道筛选：公司属性选项"""
    return {"options": COMPANY_TYPES}


@app.get("/api/options/job-natures")
async def get_job_nature_options():
    """赛道筛选：岗位实质选项"""
    return {"options": JOB_NATURES}


@app.get("/api/options/track-confidence")
async def get_track_confidence_options():
    """赛道筛选：置信度选项（全部/高/中）"""
    return {"options": [{"value": "", "label": "全部"}, {"value": "中", "label": "中及以上"}, {"value": "高", "label": "仅高"}]}


@app.get("/api/options/job-directions")
async def get_job_direction_options():
    """赛道筛选：岗位方向选项"""
    options = [{"value": d, "label": JOB_DIRECTION_LABELS.get(d, d)} for d in JOB_DIRECTIONS]
    return {"options": options}


@app.get("/api/options/preference-form")
async def get_preference_form_options():
    """求职偏好表单：公司属性、岗位实质、岗位方向、城市等选项"""
    cities = [{"value": name, "label": name} for _, name in COMMON_CITIES] + [{"value": "远程", "label": "远程"}]
    track_opts = (
        [{"value": c, "label": f"{l}（岗位实质）"} for c, l in JOB_NATURE_PREF_CODES]
        + [{"value": d, "label": f"{JOB_DIRECTION_LABELS.get(d, d)}（岗位方向）"} for d in JOB_DIRECTIONS]
    )
    return {
        "company_types": [{"value": c, "label": l} for c, l in COMPANY_TYPE_PREF_CODES],
        "job_natures": [{"value": c, "label": l} for c, l in JOB_NATURE_PREF_CODES],
        "job_directions": [{"value": d, "label": JOB_DIRECTION_LABELS.get(d, d)} for d in JOB_DIRECTIONS],
        "track_options": track_opts,
        "cities": cities,
    }


@app.get("/api/options/source-keywords")
async def get_source_keyword_options():
    """赛道筛选与匹配评分：历史抓取关键词（jobs.source_keyword，用于快捷标签与「开始匹配」下拉）"""
    if get_source_keywords is None:
        raise HTTPException(status_code=503, detail="数据库模块不可用")
    try:
        keywords = get_source_keywords()
        return {"keywords": keywords}
    except Exception as e:
        logger.error(f"获取搜索关键词失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/track-label/start")
async def start_track_label(request: TrackLabelRequest):
    """启动赛道标注任务（对 DB 中已有职位描述的岗位打公司属性/岗位实质/置信度）"""
    try:
        task_id = start_track_label_task_service(
            model_id=request.model_id or "",
            only_unlabeled=request.only_unlabeled,
        )
        return {"task_id": task_id, "message": "赛道标注任务已启动"}
    except Exception as e:
        logger.error(f"启动赛道标注任务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/jobs/by-track")
async def filter_jobs_by_track(request: TrackFilterRequest):
    """按赛道条件筛选已标注岗位，返回列表与总数"""
    if get_jobs_by_track is None:
        raise HTTPException(status_code=503, detail="数据库模块不可用")
    try:
        company_types = request.company_types or []
        job_natures = request.job_natures or []
        job_direction_primaries = request.job_direction_primaries or []
        source_keywords = [k.strip() for k in (request.source_keywords or []) if k and k.strip()]
        if not company_types:
            company_types = None
        if not job_natures:
            job_natures = None
        if not job_direction_primaries:
            job_direction_primaries = None
        if not source_keywords:
            source_keywords = None
        min_confidence = (request.min_confidence or "").strip() or None
        jobs = get_jobs_by_track(
            company_types=company_types,
            job_natures=job_natures,
            job_direction_primaries=job_direction_primaries,
            min_confidence=min_confidence,
            source_keywords=source_keywords,
        )
        return {"jobs": jobs, "total": len(jobs)}
    except Exception as e:
        logger.error(f"赛道筛选失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/jobs/export")
async def export_jobs_by_track(request: TrackFilterRequest, format: str = "xlsx"):
    """按赛道条件筛选并导出为 Excel 或 JSON 文件"""
    if get_jobs_by_track is None:
        raise HTTPException(status_code=503, detail="数据库模块不可用")
    try:
        company_types = request.company_types or []
        job_natures = request.job_natures or []
        job_direction_primaries = request.job_direction_primaries or []
        source_keywords = [k.strip() for k in (request.source_keywords or []) if k and k.strip()]
        if not company_types:
            company_types = None
        if not job_natures:
            job_natures = None
        if not job_direction_primaries:
            job_direction_primaries = None
        if not source_keywords:
            source_keywords = None
        min_confidence = (request.min_confidence or "").strip() or None
        jobs = get_jobs_by_track(
            company_types=company_types,
            job_natures=job_natures,
            job_direction_primaries=job_direction_primaries,
            min_confidence=min_confidence,
            source_keywords=source_keywords,
        )
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if format == "json":
            import json
            from fastapi.responses import Response
            return Response(
                content=json.dumps(jobs, ensure_ascii=False, indent=2),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename=jobs_filtered_{ts}.json"},
            )
        # Excel
        if not jobs:
            raise HTTPException(status_code=400, detail="没有符合条件的数据可导出")
        df = pd.DataFrame(jobs)
        df = df.drop(columns=["direction_detail", "job_direction_secondary"], errors="ignore")
        # 列名映射为中文（DB 字段名 -> 导出列名）
        column_names = {
            "job_id": "岗位ID",
            "job_name": "岗位名称",
            "source_keyword": "搜索关键词",
            "company_name": "公司名称",
            "company_industry": "公司行业",
            "company_scale": "公司规模",
            "company_intro": "公司介绍",
            "salary_desc": "薪资范围",
            "job_url": "岗位链接",
            "job_desc": "职位描述",
            "company_type": "公司属性",
            "job_nature": "岗位实质",
            "job_direction_primary": "岗位方向",
            "track_confidence": "置信度",
            "city_name": "工作地点",
            "experience": "工作经验",
            "job_tags": "职位标签",
            "job_requirements": "职位要求",
            "track_labeled_at": "标注时间",
        }
        df = df.rename(columns=column_names)
        bio = BytesIO()
        df.to_excel(bio, index=False, engine="openpyxl")
        bio.seek(0)
        return StreamingResponse(
            bio,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=jobs_filtered_{ts}.xlsx"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"导出失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    # 使用导入字符串以支持 reload 功能；access_log=False 避免轮询接口刷屏
    uvicorn.run(
        "web_console:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        access_log=False,
    )
