"""
Boss 直聘岗位详情页爬取模块 - API 方式
使用监听 API 接口的方式，与列表页爬取原理一致
"""
import time
import logging
from typing import Dict, Optional

try:
    from DrissionPage import ChromiumPage
    HAS_DRISSIONPAGE = True
except ImportError:
    HAS_DRISSIONPAGE = False
    print("❌ DrissionPage 未安装，请运行: pip install drissionpage")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DetailCrawlerAPI:
    """岗位详情页爬虫类 - API 方式"""
    
    def __init__(self, page: Optional[ChromiumPage] = None):
        """
        初始化详情页爬虫（API 方式）
        
        Args:
            page: ChromiumPage 实例（必须提供，复用浏览器实例）
        """
        if not HAS_DRISSIONPAGE:
            raise ImportError("DrissionPage 未安装，请运行: pip install drissionpage")
        
        if not page:
            raise ValueError("DetailCrawlerAPI 需要提供 page 参数（复用浏览器实例）")
        
        self.page = page
        logger.info("✅ 详情页 API 爬虫初始化成功")
    
    def crawl_job_detail_by_api(self, security_id: str, lid: str, job_url: str = "") -> Dict:
        """
        通过 API 方式爬取岗位详情页
        
        Args:
            security_id: 岗位 securityId（从列表页 API 返回的数据中获取）
            lid: 岗位 lid（从列表页 API 返回的数据中获取）
            job_url: 岗位详情页 URL（可选，用于日志）
        
        Returns:
            包含岗位详细信息的字典
        """
        result = {
            "url": job_url,
            "职位描述": "",
            "爬取时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "爬取成功": False,
            "错误信息": ""
        }
        
        if not security_id or not lid:
            result["错误信息"] = "缺少 securityId 或 lid 参数"
            logger.error(f"缺少参数: securityId={bool(security_id)}, lid={bool(lid)}")
            return result
        
        try:
            logger.info(f"通过 API 获取岗位详情: securityId={security_id[:20]}..., lid={lid[:20]}...")
            
            # 构建详情页 API URL
            api_url = f"https://www.zhipin.com/wapi/zpgeek/job/card.json?securityId={security_id}&lid={lid}&sessionId="
            
            # 启动监听，监听 card.json API（先启动监听）
            logger.info("启动监听（监听 card.json API）...")
            self.page.listen.start('card.json')
            time.sleep(0.5)  # 等待监听启动完成
            
            # 访问详情页 URL（触发 API 请求）
            if job_url:
                self.page.get(job_url)
            else:
                # 如果没有 URL，直接访问 API（但可能需要先访问页面建立会话）
                self.page.get(f"https://www.zhipin.com/job_detail/{security_id}.html")
            
            time.sleep(1)  # 等待页面开始加载
            
            # 等待并捕获 API 响应（增加超时时间）
            try:
                resp = self.page.listen.wait(timeout=15)
                
                if not resp:
                    logger.warning("未捕获到详情页 API 响应")
                    result["错误信息"] = "未捕获到 API 响应"
                    return result
                
                logger.info(f"✅ 捕获到详情页 API 响应: {resp.url[:80]}...")
                
                # 解析 JSON 数据
                json_data = resp.response.body
                
                if not isinstance(json_data, dict):
                    logger.error("API 响应不是字典格式")
                    result["错误信息"] = "API 响应格式错误"
                    return result
                
                code = json_data.get('code', 0)
                if code != 0:
                    logger.error(f"API 返回错误码: {code}, 消息: {json_data.get('message', '')}")
                    result["错误信息"] = f"API 错误: {code}"
                    return result
                
                # 提取详情页数据
                zp_data = json_data.get('zpData', {})
                
                # 只提取职位描述（用户只需要这个字段）
                job_info = zp_data.get('jobInfo', {})
                if job_info:
                    # 职位描述可能在 jobInfo 中，尝试多个可能的字段名
                    job_desc = (
                        job_info.get('jobDetail', '') or 
                        job_info.get('description', '') or 
                        job_info.get('jobDesc', '') or
                        job_info.get('detail', '') or
                        zp_data.get('jobDetail', '') or
                        zp_data.get('description', '')
                    )
                    result["职位描述"] = job_desc
                
                # 如果提取到了职位描述，认为成功
                if result["职位描述"]:
                    result["爬取成功"] = True
                    logger.info(f"✅ 通过 API 成功获取岗位详情")
                else:
                    result["错误信息"] = "API 返回的数据中没有找到有效信息"
                    logger.warning(f"⚠️ API 返回的数据中没有找到有效信息")
                    # 保存原始数据用于调试
                    try:
                        import json
                        with open('detail_api_raw_data.json', 'w', encoding='utf-8') as f:
                            json.dump(json_data, f, indent=2, ensure_ascii=False)
                        logger.info("已保存原始 API 数据到 detail_api_raw_data.json")
                    except:
                        pass
                
            except RuntimeError as e:
                if "监听未启动" in str(e):
                    logger.error("监听未启动，无法获取 API 数据")
                    result["错误信息"] = "监听未启动"
                else:
                    raise
            finally:
                # 停止监听
                try:
                    self.page.listen.stop()
                except:
                    pass
            
        except Exception as e:
            logger.error(f"通过 API 爬取岗位详情页失败: {e}", exc_info=True)
            result["错误信息"] = str(e)
            result["爬取成功"] = False
        
        return result
