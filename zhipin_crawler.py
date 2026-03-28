"""
使用 DrissionPage 爬取 Boss 直聘岗位信息
基于监听 API 接口的方式，比 RPC + API 方案更简单高效

参考文章：https://blog.csdn.net/weixin_43856625/article/details/155571228
"""
import json
import time
import logging
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs

# 配置日志（必须在导入其他模块之前）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 导入详情页爬取功能（只使用HTML方式，API方式已移除）
try:
    from detail_crawler import DetailCrawler
    HAS_DETAIL_CRAWLER = True
    logger.info("✅ 详情页爬取模块导入成功（使用 HTML 解析方式）")
except ImportError as e:
    HAS_DETAIL_CRAWLER = False
    logger.warning(f"⚠️ detail_crawler 模块未找到，详情页爬取功能将不可用: {e}")

try:
    from DrissionPage import ChromiumPage
    HAS_DRISSIONPAGE = True
except ImportError:
    HAS_DRISSIONPAGE = False
    print("❌ DrissionPage 未安装，请运行: pip install drissionpage")

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("⚠️ pandas 未安装，将无法导出 Excel。请运行: pip install pandas")

# 导入城市、学历、工作经验和薪资代码映射
try:
    from city_codes import CITY_CODE_MAP, CITY_NAME_TO_CODE, get_city_code, get_city_name, COMMON_CITIES
    from degree_codes import DEGREE_CODE_MAP, DEGREE_NAME_TO_CODE, get_degree_code, get_degree_name, COMMON_DEGREES
    from experience_codes import EXPERIENCE_CODE_MAP, EXPERIENCE_NAME_TO_CODE, get_experience_code, get_experience_name, COMMON_EXPERIENCES
    from salary_codes import SALARY_CODE_MAP, SALARY_NAME_TO_CODE, get_salary_code, get_salary_name, COMMON_SALARIES
    HAS_CODE_MAPS = True
except ImportError as e:
    HAS_CODE_MAPS = False
    logger.warning(f"⚠️ 代码映射未找到: {e}")


class ZhipinCrawler:
    """Boss 直聘爬虫类"""
    
    def __init__(self, headless: bool = False):
        """
        初始化爬虫
        
        Args:
            headless: 是否使用无头模式（不显示浏览器窗口）
        """
        if not HAS_DRISSIONPAGE:
            raise ImportError("DrissionPage 未安装，请运行: pip install drissionpage")
        
        # 创建浏览器页面对象
        # DrissionPage 会自动管理浏览器驱动，无需手动配置
        self.page = ChromiumPage()
        self.headless = headless
        
        logger.info("✅ DrissionPage 爬虫初始化成功")
    
    def crawl_jobs(self, keyword: str, city: str = "100010000", degree: str = "", experience: str = "", salary: str = "", max_pages: int = 5, 
                   crawl_details: bool = False, enable_llm_filter: bool = False, model_id: Optional[str] = None, task_id: Optional[str] = None) -> List[Dict]:
        """
        爬取岗位数据
        
        Args:
            keyword: 职位关键词（如 "Python 开发"）
            city: 城市代码（默认 100010000=全国）
            degree: 学历代码（如 203=本科，空字符串表示不限）
            experience: 工作经验代码（如 104=1-3年，默认101=不限经验）
            salary: 薪资代码（如 404=5-10k，空字符串表示不限）
            max_pages: 最多爬取的页数
            crawl_details: 是否爬取岗位详情页（包含职位描述、薪资详情、公司介绍等）
            enable_llm_filter: 是否在详情页爬取前启用 LLM 语义过滤（剔除与搜索目标不相关的岗位）
            model_id: LLM 过滤使用的模型 ID（supermind/deepseek_chat/deepseek_reasoner），仅当 enable_llm_filter=True 时有效
        
        Returns:
            岗位数据列表
        """
        jobs = []
        
        try:
            # 记录爬取参数（已经在main函数中记录过，这里再次确认）
            city_name = get_city_name(city) if HAS_CODE_MAPS else city
            degree_name = get_degree_name(degree) if HAS_CODE_MAPS and degree else '不限'
            experience_name = get_experience_name(experience) if HAS_CODE_MAPS and experience else '不限经验'
            salary_name = get_salary_name(salary) if HAS_CODE_MAPS and salary else '不限薪资'
            logger.info(f"开始爬取: keyword={keyword}, city={city}({city_name}), degree={degree}({degree_name}), experience={experience}({experience_name}), salary={salary}({salary_name}), max_pages={max_pages}, crawl_details={crawl_details}")
            logger.info(f"详情页爬取模块状态: HAS_DETAIL_CRAWLER={HAS_DETAIL_CRAWLER}")
            
            # 构建搜索 URL
            search_url = f'https://www.zhipin.com/web/geek/job?query={keyword}&city={city}'
            if degree:
                search_url += f'&degree={degree}'
            if experience:
                search_url += f'&experience={experience}'
            if salary:
                search_url += f'&salary={salary}'
            logger.info(f"访问搜索页面: {search_url}")
            
            # 检查是否已经访问过页面（Web 操作台模式下，外部已经访问并处理了登录）
            import sys
            is_web_mode = task_id is not None  # 如果有 task_id，说明是 Web 模式
            
            logger.info(f"[爬虫] 检查页面访问状态: is_web_mode={is_web_mode}, task_id={task_id}, _page_already_visited={hasattr(self, '_page_already_visited')}")
            
            if not is_web_mode or not hasattr(self, '_page_already_visited'):
                # 命令行模式，或者 Web 模式下首次访问，需要访问页面
                logger.info("[爬虫] 需要访问页面（命令行模式或首次访问）")
                self.page.get(search_url)
                time.sleep(3)  # 等待页面加载
                self._page_already_visited = True
                
                # 检查是否需要登录
                page_text = self.page.html
                if '登录' in page_text or 'login' in page_text.lower():
                    logger.warning("⚠️ 检测到登录页面")
                    if is_web_mode and task_id:
                        # Web 模式：使用任务管理器的确认机制
                        logger.info("[爬虫] Web 模式：使用任务管理器等待用户确认...")
                        from task_manager import task_manager
                        message = "⚠️ 检测到需要登录，请在浏览器中手动登录，登录完成后点击下方确认按钮继续"
                        confirmed = task_manager.wait_for_confirm(task_id, message, timeout=None)
                        if not confirmed:
                            raise Exception("用户未确认登录，任务已取消")
                        logger.info("[爬虫] 用户已确认登录，重新访问搜索页面...")
                        self.page.get(search_url)
                        time.sleep(3)
                    else:
                        # 命令行模式：等待用户输入
                        logger.info("💡 提示：请在浏览器中手动登录，登录完成后按 Enter 继续...")
                        input("登录完成后按 Enter 继续...")
                        # 重新访问搜索页面
                        self.page.get(search_url)
                        time.sleep(3)
            else:
                # Web 模式下，页面已经访问过，直接使用当前页面
                logger.info("[爬虫] 页面已访问，跳过重复访问（Web 模式），直接使用当前页面")
                logger.info(f"[爬虫] 当前页面URL: {self.page.url}")
                
                # 确保页面已经加载完成
                try:
                    page_text = self.page.html
                    logger.info(f"[爬虫] 当前页面内容长度: {len(page_text)}")
                    if len(page_text) < 100:
                        logger.warning("[爬虫] 页面内容过短，可能未加载完成，等待3秒...")
                        time.sleep(3)
                except Exception as e:
                    logger.warning(f"[爬虫] 获取页面内容异常: {e}，继续执行")
            
            # 启动监听：监听包含 joblist.json 的 API 请求
            logger.info("启动网络监听（监听 joblist.json API）...")
            self.page.listen.start('joblist.json')
            
            # 循环爬取多页数据
            try:
                for page_num in range(max_pages):
                    logger.info(f"正在采集第 {page_num + 1} 页数据...")
                    
                    try:
                        # 1. 滚动到页面底部，触发下一页数据加载
                        logger.info("滚动到页面底部，触发数据加载...")
                        self.page.scroll.to_bottom()
                        time.sleep(2)  # 等待数据加载
                        
                        # 2. 等待并捕获 API 响应
                        logger.info("等待 API 响应...")
                        try:
                            resp = self.page.listen.wait(timeout=15)
                        except RuntimeError as e:
                            if "监听未启动" in str(e) or "已停止" in str(e):
                                logger.warning("监听已停止，重新启动监听...")
                                self.page.listen.start('joblist.json')
                                resp = self.page.listen.wait(timeout=15)
                            else:
                                raise
                        
                        if not resp:
                            logger.warning(f"第 {page_num + 1} 页：未捕获到 API 响应")
                            # 尝试继续，可能是数据已经加载完成
                            time.sleep(3)
                            continue
                        
                        logger.info(f"✅ 捕获到 API 响应: {resp.url[:80]}...")
                        
                        # 3. 解析 JSON 数据
                        try:
                            json_data = resp.response.body  # 直接获取解析后的字典
                            
                            # 检查响应格式
                            if not isinstance(json_data, dict):
                                logger.warning(f"第 {page_num + 1} 页：响应不是字典格式")
                                # 保存原始响应用于调试
                                try:
                                    with open('boss_raw_response.txt', 'w', encoding='utf-8') as f:
                                        f.write(str(resp.response.body))
                                    logger.info("原始响应已保存到 boss_raw_response.txt")
                                except:
                                    pass
                                continue
                            
                            # 提取职位列表
                            zp_data = json_data.get('zpData', {})
                            job_list = zp_data.get('jobList', [])
                            
                            # 保存原始数据用于调试（检查是否包含详情页 API 所需参数）
                            if page_num == 0:  # 只保存第一页的数据
                                try:
                                    with open('boss_raw_data.json', 'w', encoding='utf-8') as f:
                                        json.dump(json_data, f, indent=2, ensure_ascii=False)
                                    logger.debug("已保存原始数据到 boss_raw_data.json（用于调试）")
                                except:
                                    pass
                            
                            if not job_list:
                                logger.info(f"第 {page_num + 1} 页：没有更多岗位数据")
                                break
                            
                            logger.info(f"✅ 第 {page_num + 1} 页：获取到 {len(job_list)} 个岗位")
                            logger.info(f"详情页爬取设置: crawl_details={crawl_details}, HAS_DETAIL_CRAWLER={HAS_DETAIL_CRAWLER}")
                            
                            # 4. 提取岗位信息
                            for job in job_list:
                                # 处理工作地点：城市+区域+商圈（如"北京-朝阳区-望京"）
                                city_name = job.get('cityName', '')
                                area_district = job.get('areaDistrict', '')
                                business_district = job.get('businessDistrict', '')
                                
                                work_location_parts = []
                                if city_name:
                                    work_location_parts.append(city_name)
                                if area_district:
                                    work_location_parts.append(area_district)
                                if business_district:
                                    work_location_parts.append(business_district)
                                
                                work_location = '-'.join(work_location_parts) if work_location_parts else ''
                                
                                # 构建岗位详情页 URL
                                encrypt_job_id = job.get('encryptJobId', '')
                                job_url = f"https://www.zhipin.com/job_detail/{encrypt_job_id}.html" if encrypt_job_id else ''
                                
                                # 提取详情页 API 所需的参数（如果存在）
                                security_id = job.get('securityId', '')
                                lid = job.get('lid', '') or zp_data.get('lid', '')  # lid 可能在 job 中，也可能在 zpData 中
                                
                                # 提取城市代码和学历代码（如果存在）
                                job_city_code = str(job.get('city', '')) if job.get('city') else ''
                                
                                # 提取核心字段，存储为字典（岗位ID 用于 DB 去重）
                                job_info = {
                                    '岗位ID': encrypt_job_id,
                                    '岗位名称': job.get('jobName', ''),
                                    '工作地点': work_location,
                                    '城市代码': job_city_code,  # 保存城市代码
                                    '学历要求': job.get('jobDegree', ''),
                                    '学历代码': get_degree_code(job.get('jobDegree', '')) if HAS_CODE_MAPS else '',  # 从学历名称转换为代码
                                    '工作经验': job.get('jobExperience', ''),
                                    '薪资范围': job.get('salaryDesc', ''),
                                    '公司名称': job.get('brandName', ''),
                                    '职位标签': ','.join(job.get('jobLabels', [])),  # 列表转字符串
                                    '职位要求': ' '.join(job.get('skills', [])),  # 技能要求拼接为字符串
                                    '招聘人姓名': job.get('bossName', ''),
                                    '招聘人职位': job.get('bossTitle', ''),
                                    '公司行业': job.get('brandIndustry', ''),
                                    '公司规模': job.get('brandScaleName', ''),
                                    # 额外字段：岗位详情页 URL 和 API 参数
                                    '岗位链接': job_url,
                                    '_securityId': security_id,  # 用于详情页 API（内部使用，不导出到 Excel）
                                    '_lid': lid,  # 用于详情页 API（内部使用，不导出到 Excel）
                                }
                                
                                # 注意：详情页爬取将在所有列表页爬取完成后统一进行，避免监听功能冲突
                                
                                logger.debug(f"提取岗位: {job_info['岗位名称']}")
                                jobs.append(job_info)
                            
                            # 5. 检查是否还有更多页
                            has_more = zp_data.get('hasMore', False)
                            if not has_more:
                                logger.info("已获取所有页面数据")
                                break
                            
                            # 6. 翻页等待，避免请求过于频繁
                            if page_num < max_pages - 1:  # 最后一页不需要等待
                                logger.info(f"第 {page_num + 1} 页采集完成，等待 3 秒后继续...")
                                time.sleep(3)
                            
                        except KeyError as e:
                            logger.error(f"第 {page_num + 1} 页：解析 JSON 数据失败，缺少字段: {e}")
                            # 保存原始 JSON 用于调试
                            try:
                                with open('boss_raw_data.json', 'w', encoding='utf-8') as f:
                                    json.dump(json_data, f, indent=4, ensure_ascii=False)
                                logger.info("原始数据已保存到 boss_raw_data.json")
                            except:
                                pass
                            # continue 会继续下一页
                            continue
                        except Exception as e:
                            logger.error(f"第 {page_num + 1} 页：处理数据时出错: {e}", exc_info=True)
                            # continue 会继续下一页
                            continue
                        
                    except KeyboardInterrupt:
                        # 用户按 Ctrl+C 中断列表页爬取
                        logger.warning(f"\n⚠️ 用户中断列表页爬取（Ctrl+C）")
                        raise  # 重新抛出，让外层处理
                    except Exception as e:
                        logger.error(f"第 {page_num + 1} 页爬取失败: {e}", exc_info=True)
                        time.sleep(3)
                        continue
            except KeyboardInterrupt:
                # 用户中断列表页爬取
                logger.warning(f"\n⚠️ 用户中断列表页爬取（Ctrl+C）")
                raise  # 重新抛出，让外层处理
            
            # LLM 语义过滤：在详情页爬取前，剔除与搜索目标不相关的岗位（无论是否爬取详情页均可生效）
            if enable_llm_filter and jobs:
                try:
                    from job_list_filter import filter_jobs_by_semantic_match
                    original_count = len(jobs)
                    jobs = filter_jobs_by_semantic_match(keyword, jobs, model_id=model_id, task_id=task_id)
                    logger.info(f"LLM 过滤：原 {original_count} 条 → 保留 {len(jobs)} 条，删除 {original_count - len(jobs)} 条")
                except Exception as e:
                    logger.warning(f"LLM 语义过滤失败，跳过过滤继续爬取: {e}")
                    # 安全降级：保留全部岗位
            
            # 如果需要爬取详情页，在所有列表页爬取完成后统一爬取
            if crawl_details and HAS_DETAIL_CRAWLER and jobs:
                # 与本地数据库比对：已在库中有职位描述的岗位不再请求详情页，直接复用（省流量与时间）
                detail_cache: Dict = {}
                try:
                    from db import init_db, get_job_detail_cache_for_ids

                    init_db()
                    id_list = []
                    for _j in jobs:
                        _jid = str(
                            _j.get("encryptJobId")
                            or _j.get("岗位ID")
                            or _j.get("_encryptJobId")
                            or ""
                        )
                        if _jid:
                            id_list.append(_jid)
                    detail_cache = get_job_detail_cache_for_ids(id_list)
                    if detail_cache:
                        logger.info(
                            f"数据库中已有 {len(detail_cache)} 个岗位的详情，将跳过详情页爬取并复用库中数据"
                        )
                except Exception as _db_e:
                    logger.warning(f"查询数据库详情缓存失败，将全部爬取详情页: {_db_e}")

                need_network = sum(
                    1
                    for _j in jobs
                    if str(_j.get("encryptJobId") or _j.get("岗位ID") or _j.get("_encryptJobId") or "")
                    not in detail_cache
                )
                logger.info(f"\n开始爬取详情页：共 {len(jobs)} 条，其中需联网爬取约 {need_network} 条...")
                logger.info("💡 提示：按 Ctrl+C 可以随时停止爬取")
                try:
                    # 停止监听，避免干扰详情页爬取
                    try:
                        self.page.listen.stop()
                        logger.info("已停止列表页监听")
                    except:
                        pass
                    
                    # 直接使用 HTML 解析方式（API方式已移除，因为总是失败）
                    detail_crawler_html = DetailCrawler(page=self.page) if HAS_DETAIL_CRAWLER else None
                    
                    if not detail_crawler_html:
                        logger.error("❌ 详情页爬虫未初始化，无法爬取详情页")
                        for job_info in jobs:
                            job_info['职位描述'] = ''
                            job_info['公司介绍'] = ''
                    else:
                        crawled_count = 0  # 记录已爬取的详情页数量
                        reused_count = 0  # 从数据库复用的条数
                        
                        try:
                            for i, job_info in enumerate(jobs, 1):
                                job_url = job_info.get('岗位链接', '')
                                jid = str(
                                    job_info.get("encryptJobId")
                                    or job_info.get("岗位ID")
                                    or job_info.get("_encryptJobId")
                                    or ""
                                )

                                if jid and jid in detail_cache:
                                    cached = detail_cache[jid]
                                    job_info["职位描述"] = cached.get("job_desc") or ""
                                    job_info["公司介绍"] = cached.get("company_intro") or ""
                                    reused_count += 1
                                    logger.info(
                                        f"[{i}/{len(jobs)}] 跳过详情爬取（库中已有）: {job_info.get('岗位名称', '')}"
                                    )
                                    continue

                                if not job_url:
                                    logger.warning(f"[{i}/{len(jobs)}] 跳过：缺少岗位链接")
                                    job_info['职位描述'] = ''
                                    job_info['公司介绍'] = ''
                                    continue
                                
                                try:
                                    logger.info(f"[{i}/{len(jobs)}] 正在爬取详情页: {job_info['岗位名称']}")
                                    
                                    # 直接使用 HTML 解析方式（更快更稳定）
                                    detail_result = detail_crawler_html.crawl_job_detail(job_url)
                                
                                    # 合并详情页信息到 job_info（职位描述 + 公司介绍）
                                    if detail_result and detail_result.get('爬取成功'):
                                        job_info['职位描述'] = detail_result.get('职位描述', '')
                                        job_info['公司介绍'] = detail_result.get('公司介绍', '')
                                        logger.info(f"✅ [{i}/{len(jobs)}] 详情页爬取成功: {job_info['岗位名称']}")
                                        crawled_count += 1
                                    else:
                                        error_msg = detail_result.get('错误信息', '') if detail_result else '未获取到详情页数据'
                                        logger.warning(f"⚠️ [{i}/{len(jobs)}] 详情页爬取失败: {job_info['岗位名称']}, 错误: {error_msg}")
                                        # 即使失败也添加空字段，保持数据结构一致
                                        job_info['职位描述'] = ''
                                        job_info['公司介绍'] = ''
                                    
                                    # 添加延迟，避免请求过快
                                    if i < len(jobs):  # 最后一个不需要延迟
                                        time.sleep(1)
                                    
                                except KeyboardInterrupt:
                                    # 用户按 Ctrl+C 中断
                                    logger.warning(f"\n⚠️ 用户中断爬取（Ctrl+C）")
                                    logger.info(f"已爬取 {crawled_count}/{len(jobs)} 个岗位的详情页")
                                    raise  # 重新抛出，让外层处理
                                except Exception as e:
                                    logger.error(f"爬取详情页时出错: {job_info['岗位名称']}, 错误: {e}")
                                    # 添加空字段，保持数据结构一致
                                    job_info['职位描述'] = ''
                                    job_info['公司介绍'] = ''
                        
                            logger.info(
                                f"✅ 详情页处理完成！联网爬取 {crawled_count} 条，库中复用 {reused_count} 条"
                            )
                        except KeyboardInterrupt:
                            # 用户中断详情页爬取
                            logger.warning(f"\n⚠️ 用户中断详情页爬取（Ctrl+C）")
                            logger.info(f"已爬取 {crawled_count}/{len(jobs)} 个岗位的详情页")
                            raise  # 重新抛出，让外层处理
                        except Exception as e:
                            logger.error(f"批量爬取详情页时出错: {e}", exc_info=True)
                except KeyboardInterrupt:
                    raise  # 重新抛出 KeyboardInterrupt，让外层处理
                except Exception as e:
                    logger.error(f"批量爬取详情页时出错: {e}", exc_info=True)
            
            logger.info(f"✅ 爬取完成！共获取 {len(jobs)} 个岗位")
            return jobs
            
        except Exception as e:
            logger.error(f"爬取失败: {e}", exc_info=True)
            return jobs
        finally:
            # 停止监听
            try:
                self.page.listen.stop()
            except:
                pass
    
    def save_to_excel(self, jobs: List[Dict], filename: Optional[str] = None):
        """
        保存数据到 Excel 文件
        
        Args:
            jobs: 岗位数据列表
            filename: 输出文件路径（可选，可以是完整路径或文件名）
        
        Returns:
            保存的文件路径
        """
        if not HAS_PANDAS:
            logger.error("pandas 未安装，无法导出 Excel")
            return None
        
        if not jobs:
            logger.warning("没有数据可保存")
            return None
        
        import os
        from pathlib import Path
        
        # 如果没有指定文件名，使用第一个岗位的关键词 + 时间戳
        if not filename:
            keyword = jobs[0].get('岗位名称', '职位')[:20] if jobs else '职位'
            # 清理文件名中的非法字符
            keyword = keyword.replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
            # 添加时间戳（格式：YYYYMMDD_HHMMSS）
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"boss_{keyword}_{timestamp}.xlsx"
        
        try:
            # 准备导出数据（排除内部使用的字段）
            export_jobs = []
            for job in jobs:
                export_job = {k: v for k, v in job.items() if not k.startswith('_')}
                export_jobs.append(export_job)
            
            # 将列表数据转换为 DataFrame
            df = pd.DataFrame(export_jobs)
            
            # 处理文件路径（支持完整路径）
            file_path = Path(filename)
            if not file_path.is_absolute():
                # 相对路径，转换为绝对路径
                file_path = Path.cwd() / file_path
            
            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 如果文件已存在且被占用，尝试添加序号
            if file_path.exists():
                try:
                    # 尝试打开文件，检查是否被占用
                    with open(file_path, 'r+b'):
                        pass
                except PermissionError:
                    # 文件被占用，添加序号
                    base_name = file_path.stem
                    counter = 1
                    while file_path.exists():
                        file_path = file_path.parent / f"{base_name}_{counter}.xlsx"
                        counter += 1
                    logger.warning(f"⚠️ 原文件被占用，使用新文件名: {file_path.name}")
            
            # 导出为 Excel：index=False 表示不保存行索引
            df.to_excel(str(file_path), index=False)
            logger.info(f"✅ 数据已保存到: {file_path}")
            print(f"\n📁 文件保存位置: {file_path}")
            return str(file_path.absolute())
        except PermissionError as e:
            logger.error(f"❌ 保存 Excel 失败：文件被占用或权限不足")
            logger.error(f"   文件路径: {file_path if 'file_path' in locals() else '未知'}")
            logger.error(f"   请关闭可能正在打开该文件的程序（如Excel），然后重试")
            print(f"\n❌ 错误：文件保存失败")
            print(f"   可能原因：文件正在被Excel或其他程序打开")
            print(f"   解决方法：关闭Excel后重新运行程序")
            return None
        except Exception as e:
            logger.error(f"保存 Excel 失败: {e}", exc_info=True)
            print(f"\n❌ 错误：文件保存失败: {e}")
            return None
    
    def close(self):
        """关闭浏览器"""
        if self.page:
            try:
                self.page.listen.stop()
            except:
                pass
            self.page.quit()
            logger.info("浏览器已关闭")


def main():
    """主函数：交互式爬取"""
    print("=" * 60)
    print("Boss 直聘爬虫 - 基于 DrissionPage")
    print("=" * 60)
    
    if not HAS_DRISSIONPAGE:
        print("\n❌ 请先安装依赖: pip install drissionpage")
        return
    
    try:
        # 接收用户输入：职位关键词
        keyword = input('\n请输入你想爬取的职位关键词（如：Python 开发）: ').strip()
        if not keyword:
            print("❌ 关键词不能为空")
            return
        
        # 选择城市（默认：杭州）
        city_code = "101210100"  # 默认杭州
        city_name = "杭州"
        if HAS_CODE_MAPS:
            print("\n请选择城市（输入序号或直接输入城市名称）：")
            print("  0. 全国")
            for idx, (code, name) in enumerate(COMMON_CITIES[1:], 1):  # 跳过"全国"，从1开始
                marker = " ← 默认" if code == city_code else ""
                print(f"  {idx}. {name}{marker}")
            print("  或直接输入城市名称（如：上海、苏州）")
            
            city_input = input(f'城市选择（直接回车使用默认"杭州"）: ').strip()
            if city_input:
                # 尝试作为序号解析
                try:
                    idx = int(city_input)
                    if idx == 0:
                        city_code = "100010000"
                        city_name = "全国"
                    elif 1 <= idx <= len(COMMON_CITIES) - 1:
                        city_code, city_name = COMMON_CITIES[idx]
                    else:
                        # 作为城市名称处理
                        city_code = get_city_code(city_input)
                        city_name = city_input
                except ValueError:
                    # 作为城市名称处理
                    city_code = get_city_code(city_input)
                    city_name = city_input
            else:
                # 使用默认值
                city_code = "101210100"
                city_name = "杭州"
        else:
            city_input = input('请输入城市代码（直接回车使用默认"101210100"=杭州）: ').strip()
            if city_input:
                city_code = city_input
                city_name = city_code
            else:
                city_code = "101210100"
                city_name = "杭州"
        
        # 选择学历（默认：本科）
        degree_code = "203"  # 默认本科
        degree_name = "本科"
        if HAS_CODE_MAPS:
            print("\n请选择学历要求（输入序号）：")
            for idx, (code, name) in enumerate(COMMON_DEGREES):
                marker = " ← 默认" if code == degree_code else ""
                print(f"  {idx}. {name}{marker}")
            
            degree_input = input(f'学历要求（直接回车使用默认"本科"）: ').strip()
            if degree_input:
                try:
                    idx = int(degree_input)
                    if 0 <= idx < len(COMMON_DEGREES):
                        degree_code, degree_name = COMMON_DEGREES[idx]
                    else:
                        # 作为学历名称处理
                        degree_code = get_degree_code(degree_input)
                        degree_name = degree_input
                except ValueError:
                    # 作为学历名称处理
                    degree_code = get_degree_code(degree_input)
                    degree_name = degree_input
            else:
                # 使用默认值
                degree_code = "203"
                degree_name = "本科"
        else:
            degree_input = input('请输入学历代码（直接回车使用默认"203"=本科，204=硕士，205=博士）: ').strip()
            if degree_input:
                degree_code = degree_input
                degree_name = degree_code
            else:
                degree_code = "203"
                degree_name = "本科"
        
        # 选择工作经验（默认：不限经验）
        experience_code = "101"  # 默认不限经验
        experience_name = "不限经验"
        if HAS_CODE_MAPS:
            print("\n请选择工作经验要求（输入序号）：")
            for idx, (code, name) in enumerate(COMMON_EXPERIENCES):
                marker = " ← 默认" if code == experience_code else ""
                print(f"  {idx}. {name}{marker}")
            
            experience_input = input(f'工作经验要求（直接回车使用默认"不限经验"）: ').strip()
            if experience_input:
                try:
                    idx = int(experience_input)
                    if 0 <= idx < len(COMMON_EXPERIENCES):
                        experience_code, experience_name = COMMON_EXPERIENCES[idx]
                    else:
                        # 作为经验名称处理
                        experience_code = get_experience_code(experience_input)
                        experience_name = experience_input
                except ValueError:
                    # 作为经验名称处理
                    experience_code = get_experience_code(experience_input)
                    experience_name = experience_input
            else:
                # 使用默认值
                experience_code = "101"
                experience_name = "不限经验"
        else:
            experience_input = input('请输入工作经验代码（直接回车使用默认"101"=不限经验，103=1年以内，104=1-3年，105=3-5年）: ').strip()
            if experience_input:
                experience_code = experience_input
                experience_name = experience_code
            else:
                experience_code = "101"
                experience_name = "不限经验"
        
        # 选择薪资范围（默认：不限）
        salary_code = ""  # 默认不限薪资
        salary_name = "不限薪资"
        if HAS_CODE_MAPS:
            print("\n请选择薪资范围（输入序号）：")
            for idx, (code, name) in enumerate(COMMON_SALARIES):
                marker = " ← 默认" if code == salary_code else ""
                print(f"  {idx}. {name}{marker}")
            
            salary_input = input(f'薪资范围（直接回车使用默认"不限薪资"）: ').strip()
            if salary_input:
                try:
                    idx = int(salary_input)
                    if 0 <= idx < len(COMMON_SALARIES):
                        salary_code, salary_name = COMMON_SALARIES[idx]
                    else:
                        # 作为薪资名称处理
                        salary_code = get_salary_code(salary_input)
                        salary_name = salary_input
                except ValueError:
                    # 作为薪资名称处理
                    salary_code = get_salary_code(salary_input)
                    salary_name = salary_input
            else:
                # 使用默认值
                salary_code = ""
                salary_name = "不限薪资"
        else:
            salary_input = input('请输入薪资代码（直接回车表示"不限"，402=3k以下，403=3-5k，404=5-10k，405=10-20k，406=20-50k，407=50k以上）: ').strip()
            if salary_input:
                salary_code = salary_input
                salary_name = salary_code
            else:
                salary_code = ""
                salary_name = "不限薪资"
        
        # 记录搜索条件到日志
        logger.info("=" * 60)
        logger.info("搜索条件设置：")
        logger.info(f"  职位关键词: {keyword}")
        logger.info(f"  城市: {city_name} (代码: {city_code})")
        logger.info(f"  学历要求: {degree_name} (代码: {degree_code})")
        logger.info(f"  工作经验: {experience_name} (代码: {experience_code})")
        logger.info(f"  薪资范围: {salary_name} (代码: {salary_code if salary_code else '不限'})")
        logger.info("=" * 60)
        
        try:
            max_pages = int(input('\n请输入你想爬取的页数（建议 1-10）: ').strip())
            if max_pages <= 0:
                print("❌ 页数必须大于 0")
                return
        except ValueError:
            print("❌ 请输入有效的数字")
            return
        
        # 询问是否爬取详情页（默认：是，但需要确认）
        crawl_details = True  # 默认开启
        print('\n是否爬取岗位详情页（包含职位描述）？')
        print('  💡 默认：是（推荐，可以获得完整的职位描述信息）')
        crawl_details_input = input('  确认爬取详情页？(y/n，直接回车=是): ').strip().lower()
        if crawl_details_input == 'n':
            crawl_details = False
            print("⚠️ 已取消详情页爬取，将只爬取列表页信息")
        else:
            print("✅ 将爬取详情页（包含职位描述）")
            print("💡 提示：爬取详情页会增加爬取时间，但能获得更完整的信息")
        
        print("\n💡 提示：")
        print("   1. 浏览器窗口会自动打开")
        print("   2. 如果需要登录，请在浏览器中手动登录")
        print("   3. 登录完成后，程序会自动继续爬取")
        print("   4. 如果页面提示登录，按 Enter 后手动登录")
        print("   5. 爬取过程中可以随时按 Ctrl+C 停止\n")
        
        # 创建爬虫实例
        crawler = ZhipinCrawler(headless=False)  # 显示浏览器窗口，方便调试和登录
        
        try:
            # 爬取数据
            jobs = crawler.crawl_jobs(keyword=keyword, city=city_code, degree=degree_code, experience=experience_code, salary=salary_code, max_pages=max_pages, crawl_details=crawl_details)
            
            if jobs:
                # 保存到 Excel
                crawler.save_to_excel(jobs)
                print(f"\n✅ 爬取完成！共采集 {len(jobs)} 条岗位数据")
                print(f"📁 数据已保存到 Excel 文件")
            else:
                print("\n⚠️ 未获取到任何数据，可能原因：")
                print("   1. 需要登录：请在浏览器中手动登录后重试")
                print("   2. 网络连接问题")
                print("   3. 搜索关键词无结果")
                print("   4. 被反爬虫机制拦截")
                print("\n💡 调试建议：")
                print("   - 检查浏览器窗口中的页面状态")
                print("   - 查看控制台日志输出")
                print("   - 如果解析失败，会保存原始数据到 boss_raw_data.json")
        
        except KeyboardInterrupt:
            print("\n\n⚠️ 用户中断（Ctrl+C）")
            print("程序已停止")
        finally:
            # 关闭浏览器
            try:
                crawler.close()
            except:
                pass
    
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断（Ctrl+C）")
        print("程序已停止")
    except Exception as e:
        logger.error(f"程序执行失败: {e}", exc_info=True)


if __name__ == '__main__':
    main()
