"""
Boss 直聘职位详情页：根据 HTML 判断是否在招聘（已关闭 / 在招 / 无法判定）。
检测依据来自静态源码：page_key_name=cpc_job_detail_expired、.job-status 内「职位已关闭」等。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional, Tuple
from urllib.parse import urljoin

from task_log_handler import TaskLogHandler
from task_manager import task_manager, TaskStatus
from zhipin_crawler import ZhipinCrawler

logger = logging.getLogger(__name__)

BOSS_ORIGIN = "https://www.zhipin.com"
WARMUP_SEARCH_URL = "https://www.zhipin.com/web/geek/job?query=python&city=100010000"


def normalize_job_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return urljoin(BOSS_ORIGIN, u)
    if not u.startswith("http"):
        return urljoin(BOSS_ORIGIN, u)
    return u


def detect_recruitment_status(html: str) -> Tuple[str, str]:
    """
    解析详情页 HTML，返回 (status, reason)。
    status: 'closed' | 'open' | 'unknown'
    """
    if not html or len(html) < 400:
        return "unknown", "html_too_short_or_empty"

    # 1) page_key_name（过期页为 cpc_job_detail_expired）
    m = re.search(
        r'id\s*=\s*["\']page_key_name["\'][^>]*value\s*=\s*["\']([^"\']*)["\']',
        html,
        re.I,
    )
    if not m:
        m = re.search(
            r'value\s*=\s*["\']([^"\']*)["\'][^>]*id\s*=\s*["\']page_key_name["\']',
            html,
            re.I,
        )
    if m:
        pk = (m.group(1) or "").strip()
        low = pk.lower()
        if "expired" in low:
            return "closed", f"page_key_name={pk}"
        if "job_detail" in low and "expired" not in low:
            return "open", f"page_key_name={pk}"

    # 2) .job-status 内「职位已关闭」
    if re.search(
        r'class\s*=\s*["\']job-status["\'][^>]*>[\s\S]{0,120}?职位已关闭',
        html,
    ):
        return "closed", "job-status:职位已关闭"
    if "职位已关闭" in html and "job-status" in html:
        pos = html.find("职位已关闭")
        if pos != -1:
            chunk = html[max(0, pos - 300) : pos + 80]
            if "job-status" in chunk:
                return "closed", "job-status:职位已关闭"

    # 3) 疑似登录页 / 反爬占位
    if "请稍候" in html and len(html) < 8000:
        return "unknown", "loading_or_challenge_page"
    if "短信登录" in html and "job-banner" not in html and "job_detail" not in html.lower():
        return "unknown", "likely_login_page"

    # 4) 在招弱信号：主区无「职位已关闭」且有沟通类 CTA
    head = html[:25000]
    if "职位已关闭" not in head and (
        "立即沟通" in head
        or "btn-startchat" in head
        or "btn-start" in head
        or "开聊" in head
    ):
        if "job-banner" in head or "info-primary" in head:
            return "open", "active_job_cta"

    return "unknown", "no_definitive_signal"


def run_job_expiry_check_task(
    task_id: str,
    delay_sec: float = 1.5,
    limit: int = 500,
    open_cooldown_days: int = 7,
) -> None:
    """
    使用 DrissionPage 逐条打开 job_url，解析 HTML 并更新 recruitment_status / recruitment_checked_at。
    """
    from db import (
        RECRUITMENT_CLOSED,
        RECRUITMENT_OPEN,
        RECRUITMENT_UNKNOWN,
        get_jobs_for_recruitment_check,
        update_job_recruitment_status,
        _now_iso,
    )

    handler = TaskLogHandler(task_id=task_id)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    log = logging.getLogger(__name__)
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    crawler: Optional[ZhipinCrawler] = None
    try:
        task_manager.update_status(task_id, TaskStatus.RUNNING)
        task_manager.add_log(task_id, "开始「排除过期岗位」检测任务", "INFO")
        task_manager.add_log(
            task_id,
            f"参数: 每条间隔 {delay_sec}s，最多检测 {limit} 条；"
            f"在招(open)岗位距上次检测未满 {open_cooldown_days} 天则跳过",
            "INFO",
        )

        jobs = get_jobs_for_recruitment_check(
            limit=limit, open_cooldown_days=open_cooldown_days
        )
        total = len(jobs)
        if total == 0:
            task_manager.add_log(
                task_id,
                "没有待检测岗位（均已关闭、或在招尚在冷却期内、或库中无符合条件链接）",
                "WARNING",
            )
            task_manager.update_progress(task_id, 0, 0)
            task_manager.update_result(
                task_id,
                success_count=0,
                failed_count=0,
                closed_count=0,
                open_count=0,
                unknown_count=0,
                checked_total=0,
            )
            task_manager.update_status(task_id, TaskStatus.COMPLETED)
            return

        task_manager.update_progress(task_id, 0, total)
        crawler = ZhipinCrawler(headless=False)
        page = crawler.page

        task_manager.add_log(task_id, "正在打开搜索页以检测登录态...", "INFO")
        page.get(WARMUP_SEARCH_URL)
        time.sleep(2.0)
        page_text = page.html or ""
        if "登录" in page_text or "login" in page_text.lower():
            task_manager.add_log(
                task_id,
                "检测到需登录：请在浏览器中登录 Boss，然后点击下方「确认」继续",
                "WARNING",
            )
            confirmed = task_manager.wait_for_confirm(
                task_id,
                "请在浏览器中完成 Boss 登录后点击确认，继续检测职位详情",
                timeout=None,
            )
            if not confirmed:
                task_manager.set_error(task_id, "用户未确认登录，任务已取消")
                return
            page.get(WARMUP_SEARCH_URL)
            time.sleep(2.0)

        closed_count = 0
        open_count = 0
        unknown_count = 0
        failed_count = 0
        checked = 0

        for idx, row in enumerate(jobs):
            job_id = row.get("job_id") or ""
            raw_url = row.get("job_url") or ""
            url = normalize_job_url(raw_url)
            name = (row.get("job_name") or "")[:32]
            if not url or not job_id:
                failed_count += 1
                task_manager.add_log(task_id, f"[{idx+1}/{total}] 跳过：无有效链接 job_id={job_id}", "WARNING")
                continue
            try:
                task_manager.add_log(task_id, f"[{idx+1}/{total}] 访问 {name} … {url[:80]}…", "INFO")
                page.get(url)
                time.sleep(max(0.3, float(delay_sec)))
                html = page.html or ""
                status, reason = detect_recruitment_status(html)
                now_iso = _now_iso()
                if status == "closed":
                    update_job_recruitment_status(job_id, RECRUITMENT_CLOSED, now_iso)
                    closed_count += 1
                    task_manager.add_log(
                        task_id,
                        f"  → 已关闭 ({reason})，已写入 recruitment_status=closed",
                        "INFO",
                    )
                elif status == "open":
                    update_job_recruitment_status(job_id, RECRUITMENT_OPEN, now_iso)
                    open_count += 1
                    task_manager.add_log(
                        task_id,
                        f"  → 在招 ({reason})，已写入 recruitment_status=open",
                        "INFO",
                    )
                else:
                    update_job_recruitment_status(job_id, RECRUITMENT_UNKNOWN, now_iso)
                    unknown_count += 1
                    task_manager.add_log(
                        task_id,
                        f"  → 无法判定 ({reason})，已记录检测时间并标记 unknown",
                        "WARNING",
                    )
                checked += 1
            except Exception as e:
                failed_count += 1
                logger.exception("检测单条失败")
                task_manager.add_log(task_id, f"  ✗ 请求失败: {e}", "ERROR")

            task_manager.update_progress(task_id, idx + 1, total)

        task_manager.update_result(
            task_id,
            success_count=checked,
            failed_count=failed_count,
            closed_count=closed_count,
            open_count=open_count,
            unknown_count=unknown_count,
            checked_total=checked,
        )
        task_manager.add_log(
            task_id,
            f"检测结束：在招 {open_count}，已关闭 {closed_count}，无法判定 {unknown_count}，失败 {failed_count}，合计处理 {checked} 条",
            "INFO",
        )
        task_manager.update_status(task_id, TaskStatus.COMPLETED)
    except Exception as e:
        logger.exception("排除过期岗位任务失败")
        task_manager.set_error(task_id, str(e))
        task_manager.add_log(task_id, f"任务异常: {e}", "ERROR")
    finally:
        if crawler is not None:
            try:
                crawler.close()
            except Exception:
                pass
        try:
            log.removeHandler(handler)
        except Exception:
            pass
