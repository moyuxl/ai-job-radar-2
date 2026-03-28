"""
Boss 直聘岗位详情页爬取模块
提取岗位的完整描述信息（职位描述、薪资详情、公司介绍等）
"""
from __future__ import annotations

import time
import logging
import re
from typing import Dict, Optional, List
from urllib.parse import urlparse

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


class DetailCrawler:
    """岗位详情页爬虫类"""
    
    def __init__(self, page: Optional[ChromiumPage] = None, headless: bool = False):
        """
        初始化详情页爬虫
        
        Args:
            page: 可选的 ChromiumPage 实例（如果已有浏览器实例，可以复用）
            headless: 是否使用无头模式（仅在创建新实例时有效）
        """
        if not HAS_DRISSIONPAGE:
            raise ImportError("DrissionPage 未安装，请运行: pip install drissionpage")
        
        # 如果提供了页面实例，复用；否则创建新的
        if page:
            self.page = page
            self.own_page = False
        else:
            self.page = ChromiumPage()
            self.own_page = True
            self.headless = headless
        
        logger.info("✅ 详情页爬虫初始化成功")
    
    def crawl_job_detail(self, job_url: str) -> Dict:
        """
        爬取单个岗位的详情页
        
        Args:
            job_url: 岗位详情页 URL（如：https://www.zhipin.com/job_detail/xxx.html）
        
        Returns:
            包含岗位详细信息的字典
        """
        result = {
            "url": job_url,
            "职位描述": "",
            "公司介绍": "",
            "爬取时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "爬取成功": False,
            "错误信息": ""
        }
        
        is_login_page = False  # 初始化变量
        
        try:
            logger.info(f"访问岗位详情页: {job_url}")
            
            # 访问详情页
            self.page.get(job_url)
            time.sleep(2)  # 减少等待时间，只等待2秒
            
            # 等待页面内容加载（减少超时时间）
            try:
                # 等待主要内容区域加载
                self.page.wait.ele_displayed('.job-detail-section', timeout=5)
                logger.info("页面主要内容已加载")
            except:
                logger.debug("等待页面加载超时，尝试继续...")
            
            # 改进的登录检测：检查页面标题和URL，而不是简单的文本匹配
            page_title = self.page.title.lower()
            current_url = self.page.url.lower()
            page_html = self.page.html
            
            # 检查是否真的有岗位详情内容
            has_job_detail = '.job-detail-section' in page_html or 'job-sec-text' in page_html
            
            # 更准确的登录页面判断：只有在URL明确是登录页，或者页面完全没有岗位详情内容时才认为是登录页
            is_login_page = (
                'login' in current_url or 
                'signin' in current_url or
                ('/login' in current_url) or
                (not has_job_detail and ('登录' in page_title or 'login' in page_title))
            )
            
            if is_login_page and not has_job_detail:
                logger.warning("⚠️ 检测到登录页面，且页面没有岗位详情内容")
                result["错误信息"] = "需要登录"
                return result
            else:
                logger.info("✅ 页面加载正常，开始提取数据")
            
            # 提取职位描述
            try:
                job_desc = self._extract_job_description()
                result["职位描述"] = job_desc
                if job_desc:
                    logger.info(f"✅ 提取职位描述成功（长度: {len(job_desc)} 字符）")
                else:
                    logger.warning("⚠️ 未找到职位描述")
            except Exception as e:
                logger.error(f"提取职位描述失败: {e}")
                result["错误信息"] += f"提取职位描述失败: {str(e)}; "
            
            # 提取公司介绍（若有，用于赛道标注时的公司属性判断）
            try:
                company_intro = self._extract_company_info()
                result["公司介绍"] = company_intro or ""
                if company_intro:
                    logger.debug(f"提取公司介绍成功（长度: {len(company_intro)} 字符）")
            except Exception as e:
                logger.debug(f"提取公司介绍时出错: {e}")
                result["公司介绍"] = ""
            
            # 如果提取到了职位描述，认为爬取成功
            if result["职位描述"]:
                result["爬取成功"] = True
                logger.info(f"✅ 岗位详情页爬取完成: {job_url}")
            else:
                # 如果没有提取到数据，检查是否是登录页面
                page_html_check = self.page.html if hasattr(self, 'page') else ''
                if is_login_page or (not page_html_check or '.job-detail-section' not in page_html_check):
                    result["错误信息"] = "需要登录或页面未加载完成"
                else:
                    result["错误信息"] = "未提取到任何有效信息"
                logger.warning(f"⚠️ 未提取到有效信息: {job_url}, 错误: {result['错误信息']}")
            
        except Exception as e:
            logger.error(f"爬取岗位详情页失败: {job_url}, 错误: {e}", exc_info=True)
            result["错误信息"] = str(e)
            result["爬取成功"] = False
        
        return result
    
    def _extract_job_description(self) -> str:
        """
        提取职位描述
        
        Returns:
            职位描述文本
        """
        try:
            # 方法1：查找所有 job-sec-text 元素
            job_desc_elements = self.page.eles('.job-sec-text')
            
            if not job_desc_elements:
                logger.debug("未找到 .job-sec-text 元素")
                return ""
            
            logger.debug(f"找到 {len(job_desc_elements)} 个 .job-sec-text 元素")
            
            # 查找"职位描述"标题
            try:
                # 查找包含"职位描述"的 h3 标题
                headers = self.page.eles('tag:h3')
                for header in headers:
                    if header and '职位描述' in header.text:
                        logger.debug("找到'职位描述'标题")
                        # 查找标题后的第一个 job-sec-text
                        # 尝试查找标题的父级或下一个兄弟元素
                        try:
                            # 方法：查找标题所在区域的下一个 job-sec-text
                            parent = header.parent()
                            if parent:
                                # 在父级中查找 job-sec-text
                                job_text = parent.ele('.job-sec-text', timeout=2)
                                if job_text:
                                    text = job_text.text.strip()
                                    text = re.sub(r'<br\s*/?>', '\n', text)
                                    text = re.sub(r'<[^>]+>', '', text)
                                    logger.debug(f"从标题后提取到职位描述（长度: {len(text)}）")
                                    return text.strip()
                        except:
                            pass
            except Exception as e:
                logger.debug(f"查找标题时出错: {e}")
            
            # 方法2：如果没有找到标题，使用第一个 job-sec-text（通常是职位描述）
            # 但要排除薪资详情和公司介绍
            for elem in job_desc_elements:
                try:
                    text = elem.text.strip()
                    # 检查是否是薪资详情或公司介绍
                    parent_html = elem.parent().html if elem.parent() else ''
                    if '薪资详情' in parent_html or 'salary-info' in parent_html:
                        continue
                    if '公司介绍' in parent_html or 'company-info' in parent_html:
                        continue
                    
                    # 清理 HTML 标签
                    text = re.sub(r'<br\s*/?>', '\n', text)
                    text = re.sub(r'<[^>]+>', '', text)
                    text = text.strip()
                    
                    # 如果文本长度合理（大于50字符），认为是职位描述
                    if len(text) > 50:
                        logger.debug(f"使用第一个 job-sec-text 作为职位描述（长度: {len(text)}）")
                        return text
                except:
                    continue
            
            # 方法3：如果都没找到，返回第一个元素（即使可能不完整）
            if job_desc_elements:
                text = job_desc_elements[0].text.strip()
                text = re.sub(r'<br\s*/?>', '\n', text)
                text = re.sub(r'<[^>]+>', '', text)
                logger.debug(f"使用第一个元素作为职位描述（长度: {len(text)}）")
                return text.strip()
            
            return ""
        except Exception as e:
            logger.error(f"提取职位描述时出错: {e}", exc_info=True)
            return ""
    
    def _extract_salary_info(self) -> str:
        """
        提取薪资详情
        
        Returns:
            薪资详情文本
        """
        try:
            # 查找薪资详情区域
            salary_section = self.page.ele('.salary-info', timeout=2)
            if salary_section:
                job_sec_text = salary_section.ele('.job-sec-text', timeout=2)
                if job_sec_text:
                    text = job_sec_text.text.strip()
                    # 清理 HTML 标签
                    text = re.sub(r'<br\s*/?>', '\n', text)
                    text = re.sub(r'<[^>]+>', '', text)
                    return text.strip()
            
            # 备用方法：查找所有 job-sec-text，找到包含"薪资"的
            job_desc_elements = self.page.eles('.job-sec-text')
            for elem in job_desc_elements:
                text = elem.text.strip()
                if '薪资' in text or '社保' in text:
                    text = re.sub(r'<br\s*/?>', '\n', text)
                    text = re.sub(r'<[^>]+>', '', text)
                    return text.strip()
            
            return ""
        except Exception as e:
            logger.debug(f"提取薪资详情时出错: {e}")
            return ""
    
    def _extract_company_info(self) -> str:
        """
        提取公司介绍
        
        Returns:
            公司介绍文本
        """
        try:
            # 查找公司介绍区域
            company_section = self.page.ele('.company-info-box', timeout=2)
            if company_section:
                job_sec_text = company_section.ele('.job-sec-text', timeout=2)
                if job_sec_text:
                    text = job_sec_text.text.strip()
                    # 清理 HTML 标签
                    text = re.sub(r'<br\s*/?>', '\n', text)
                    text = re.sub(r'<[^>]+>', '', text)
                    # 移除"查看全部"等链接文本
                    text = re.sub(r'查看全部.*', '', text)
                    return text.strip()
            
            return ""
        except Exception as e:
            logger.debug(f"提取公司介绍时出错: {e}")
            return ""
    
    def _extract_work_address(self) -> str:
        """
        提取工作地址
        
        Returns:
            工作地址文本
        """
        try:
            # 查找工作地址区域
            address_section = self.page.ele('.company-address', timeout=2)
            if address_section:
                location_address = address_section.ele('.location-address', timeout=2)
                if location_address:
                    return location_address.text.strip()
            
            return ""
        except Exception as e:
            logger.debug(f"提取工作地址时出错: {e}")
            return ""
    
    def _extract_business_info(self) -> Dict:
        """
        提取工商信息
        
        Returns:
            工商信息字典
        """
        business_info = {}
        
        try:
            # 查找工商信息区域
            business_section = self.page.ele('.business-info-box', timeout=2)
            if business_section:
                # 查找所有列表项
                list_items = business_section.eles('tag:li', timeout=2)
                for item in list_items:
                    text = item.text.strip()
                    # 解析格式：公司名称 xxx 或 法定代表人 xxx
                    if '：' in text or ':' in text:
                        parts = re.split(r'[：:]', text, 1)
                        if len(parts) == 2:
                            key = parts[0].strip()
                            value = parts[1].strip()
                            business_info[key] = value
                    elif '<span>' in item.html:
                        # 尝试从 HTML 中提取
                        span = item.ele('tag:span', timeout=1)
                        if span:
                            key = span.text.strip()
                            # 获取 span 后面的文本
                            value = text.replace(key, '').strip()
                            business_info[key] = value
            
            return business_info
        except Exception as e:
            logger.debug(f"提取工商信息时出错: {e}")
            return business_info
    
    def close(self):
        """关闭浏览器（仅在拥有页面实例时）"""
        if self.own_page and self.page:
            self.page.quit()
            logger.info("浏览器已关闭")


def crawl_job_detail(job_url: str, page: Optional[ChromiumPage] = None) -> Dict:
    """
    爬取单个岗位详情页（便捷函数）
    
    Args:
        job_url: 岗位详情页 URL
        page: 可选的 ChromiumPage 实例（用于批量爬取时复用浏览器）
    
    Returns:
        包含岗位详细信息的字典
    """
    crawler = DetailCrawler(page=page)
    try:
        return crawler.crawl_job_detail(job_url)
    finally:
        if page is None:  # 只有在我们自己创建的页面时才关闭
            crawler.close()


if __name__ == '__main__':
    """测试单个岗位详情页爬取"""
    import sys
    
    if len(sys.argv) < 2:
        print("使用方法: python detail_crawler.py <岗位详情页URL>")
        print("示例: python detail_crawler.py https://www.zhipin.com/job_detail/xxx.html")
        sys.exit(1)
    
    job_url = sys.argv[1]
    
    print("=" * 60)
    print("Boss 直聘岗位详情页爬取测试")
    print("=" * 60)
    
    crawler = DetailCrawler(headless=False)
    
    try:
        result = crawler.crawl_job_detail(job_url)
        
        print("\n" + "=" * 60)
        print("爬取结果:")
        print("=" * 60)
        print(f"URL: {result['url']}")
        print(f"爬取成功: {result['爬取成功']}")
        
        if result['爬取成功']:
            print(f"\n职位描述（前200字符）:")
            print(result['职位描述'][:200] + "..." if len(result['职位描述']) > 200 else result['职位描述'])
            
            print(f"\n薪资详情:")
            print(result['薪资详情'])
            
            print(f"\n公司介绍（前200字符）:")
            print(result['公司介绍'][:200] + "..." if len(result['公司介绍']) > 200 else result['公司介绍'])
            
            print(f"\n工作地址:")
            print(result['工作地址'])
            
            if result['工商信息']:
                print(f"\n工商信息:")
                for key, value in result['工商信息'].items():
                    print(f"  {key}: {value}")
        else:
            print(f"\n错误信息: {result['错误信息']}")
    
    finally:
        crawler.close()
