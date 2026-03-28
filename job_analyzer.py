"""
岗位数据分析模块
1. 数据清洗：从岗位描述中提取岗位职责、必备技能、加分技能、技术栈
2. 评分：对4个维度分别评分
3. 综合评分：计算综合得分
4. 筛选：筛选高分岗位供LLM深度分析
"""
import re
import pandas as pd
from typing import Dict, List, Tuple
import logging
import sys
import os
from datetime import datetime

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class JobAnalyzer:
    """岗位分析器"""
    
    def __init__(self):
        # 技术栈关键词（可以根据需要扩展）
        self.tech_keywords = {
            'AI/ML': ['AI', '人工智能', '机器学习', '深度学习', '神经网络', 'GPT', 'LLM', '大模型', 'NLP', '自然语言处理', '计算机视觉', 'CV'],
            'Python': ['Python', 'python', 'Django', 'Flask', 'FastAPI', 'Pandas', 'NumPy'],
            'Java': ['Java', 'java', 'Spring', 'SpringBoot'],
            '前端': ['React', 'Vue', 'Angular', 'JavaScript', 'TypeScript', 'HTML', 'CSS'],
            '数据库': ['MySQL', 'PostgreSQL', 'MongoDB', 'Redis', 'Elasticsearch'],
            '云服务': ['AWS', 'Azure', '阿里云', '腾讯云', 'Docker', 'Kubernetes'],
            '产品工具': ['Axure', 'Figma', 'Sketch', '墨刀', '原型设计'],
        }
        
        # 岗位职责关键词
        self.responsibility_keywords = [
            '负责', '参与', '主导', '推进', '规划', '设计', '开发', '优化', '维护',
            '需求分析', '产品设计', '功能设计', '用户体验', '用户研究', '竞品分析',
            '项目管理', '团队协作', '沟通协调'
        ]
        
        # 必备技能关键词（硬技能）
        self.required_skills_keywords = [
            '必须', '要求', '具备', '掌握', '熟悉', '精通', '熟练',
            '经验', '能力', '技能', '知识', '背景'
        ]
        
        # 加分技能关键词（软技能/额外技能）
        self.plus_skills_keywords = [
            '优先', '加分', '更好', '额外', 'bonus', 'plus',
            '有...经验', '了解', '接触过'
        ]
        
        # 标题终止词表（用于识别下一部分的开始）
        self.section_titles = [
            '岗位要求', '任职要求', '任职资格', '要求', '岗位条件', '职位要求',
            '加分项', '优先条件', '优先考虑', '优先',
            '薪资福利', '薪资待遇', '薪酬福利', '福利待遇',
            '公司介绍', '公司简介', '关于我们',
            '我们希望你', '你需要', '希望你', '需要',
            '岗位职责', '岗位责任', '工作内容', '职责描述', '主要职责',
        ]
    
    def to_bullets(self, text: str) -> List[str]:
        """
        将段落拆分成条目列表
        
        Args:
            text: 文本段落
        
        Returns:
            条目列表
        """
        if not text or pd.isna(text):
            return []
        
        text = str(text).strip()
        if not text:
            return []
        
        bullets = []
        
        # 先按换行分割
        lines = text.split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 检查是否是编号列表（1. 2. 3. 或 一、二、三、）
            numbered_pattern = r'^[0-9一二三四五六七八九十]+[\.、：:]\s*(.+)$'
            match = re.match(numbered_pattern, line)
            if match:
                bullets.append(match.group(1).strip())
                continue
            
            # 检查是否是项目符号（-、•、*）
            bullet_pattern = r'^[-•*]\s*(.+)$'
            match = re.match(bullet_pattern, line)
            if match:
                bullets.append(match.group(1).strip())
                continue
            
            # 检查是否包含分号，如果有则按分号分割
            if '；' in line or ';' in line:
                parts = re.split('[；;]', line)
                for part in parts:
                    part = part.strip()
                    if part:
                        bullets.append(part)
                continue
            
            # 检查是否包含句号，如果整行很长且包含多个句号，按句号分割
            if '。' in line and len(line) > 50:
                parts = re.split('。', line)
                for part in parts:
                    part = part.strip()
                    if part and len(part) > 10:  # 过滤太短的片段
                        bullets.append(part)
                continue
            
            # 如果都不匹配，整行作为一个条目
            bullets.append(line)
        
        # 过滤空条目
        bullets = [b for b in bullets if b and len(b.strip()) > 0]
        
        return bullets
    
    def extract_job_responsibilities(self, job_desc: str) -> str:
        """
        提取岗位职责（保留原文，包括标题）
        
        Args:
            job_desc: 岗位描述文本
        
        Returns:
            岗位职责文本（包含标题和完整内容）
        """
        if not job_desc or pd.isna(job_desc):
            return ''
        
        job_desc = str(job_desc)
        
        # 构建终止模式：优先使用下一标题出现作为终止
        title_pattern = '|'.join([re.escape(title) for title in self.section_titles])
        # 排除当前标题本身
        responsibility_titles = ['岗位职责', '岗位责任', '工作内容', '职责描述', '主要职责']
        
        # 查找"岗位职责"、"工作内容"等标题后的内容（保留标题）
        patterns = [
            r'(岗位职责[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(岗位责任[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(工作内容[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(职责描述[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(主要职责[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, job_desc, re.DOTALL | re.IGNORECASE)
            if match:
                text = match.group(1).strip()
                # 保留原文格式，只清理多余的空格
                text = re.sub(r'[ \t]+', ' ', text)  # 只清理空格和制表符，保留换行
                if len(text) > 20:  # 确保提取到有效内容
                    return text
        
        # 如果没有找到明确的标题，提取包含职责关键词的段落
        lines = job_desc.split('\n')
        responsibility_lines = []
        for line in lines:
            if any(keyword in line for keyword in self.responsibility_keywords):
                responsibility_lines.append(line.strip())
        
        return '\n'.join(responsibility_lines) if responsibility_lines else ''
    
    def extract_tech_stack(self, job_desc: str) -> Tuple[str, str]:
        """
        提取技术栈（类别和明细）
        
        Args:
            job_desc: 岗位描述文本
        
        Returns:
            (技术栈类别, 技术栈明细) 元组
        """
        if not job_desc or pd.isna(job_desc):
            return '', ''
        
        job_desc = str(job_desc)
        found_categories = []
        found_keywords = []
        
        # 遍历所有技术关键词
        for category, keywords in self.tech_keywords.items():
            for keyword in keywords:
                if keyword in job_desc:
                    if category not in found_categories:
                        found_categories.append(category)
                    # 记录命中的具体关键词
                    if keyword not in found_keywords:
                        found_keywords.append(keyword)
                    break
        
        categories_str = ', '.join(found_categories) if found_categories else ''
        keywords_str = ', '.join(found_keywords) if found_keywords else ''
        
        return categories_str, keywords_str
    
    def extract_required_skills(self, job_desc: str) -> str:
        """
        提取必备技能（保留原文，包括标题）
        
        Args:
            job_desc: 岗位描述文本
        
        Returns:
            必备技能文本（包含标题和完整内容）
        """
        if not job_desc or pd.isna(job_desc):
            return ''
        
        job_desc = str(job_desc)
        
        # 构建终止模式：优先使用下一标题出现作为终止
        title_pattern = '|'.join([re.escape(title) for title in self.section_titles])
        # 排除当前标题本身
        required_titles = ['岗位要求', '任职要求', '任职资格', '要求', '岗位条件', '职位要求', '我们希望你', '你需要', '希望你', '需要']
        
        # 查找"岗位要求"、"任职要求"等标题后的内容（保留标题）
        patterns = [
            r'(岗位要求[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(任职要求[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(任职资格[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(岗位条件[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(职位要求[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(我们希望你[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(你需要[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(希望你[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(要求[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, job_desc, re.DOTALL | re.IGNORECASE)
            if match:
                text = match.group(1).strip()
                # 保留原文格式，只清理多余的空格
                text = re.sub(r'[ \t]+', ' ', text)  # 只清理空格和制表符，保留换行
                if len(text) > 20:  # 确保提取到有效内容
                    return text
        
        # 如果没有找到明确的标题，提取包含必备技能关键词的段落
        lines = job_desc.split('\n')
        required_lines = []
        for line in lines:
            if any(keyword in line for keyword in self.required_skills_keywords):
                if not any(plus_keyword in line for plus_keyword in self.plus_skills_keywords):
                    required_lines.append(line.strip())
        
        return '\n'.join(required_lines) if required_lines else ''
    
    def extract_plus_skills(self, job_desc: str) -> str:
        """
        提取加分技能（保留原文，包括标题）
        
        Args:
            job_desc: 岗位描述文本
        
        Returns:
            加分技能文本（包含标题和完整内容）
        """
        if not job_desc or pd.isna(job_desc):
            return ''
        
        job_desc = str(job_desc)
        
        # 构建终止模式：优先使用下一标题出现作为终止
        title_pattern = '|'.join([re.escape(title) for title in self.section_titles])
        
        # 查找"加分项"、"优先条件"等标题后的内容（保留标题）
        patterns = [
            r'(加分项[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(优先条件[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
            r'(优先考虑[：:]\s*.*?)(?=\n\s*(?:' + title_pattern + r')[：:]|\n\n|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, job_desc, re.DOTALL | re.IGNORECASE)
            if match:
                text = match.group(1).strip()
                # 保留原文格式，只清理多余的空格
                text = re.sub(r'[ \t]+', ' ', text)  # 只清理空格和制表符，保留换行
                if len(text) > 10:  # 确保提取到有效内容
                    return text
        
        # 如果没有找到明确的标题，提取包含加分技能关键词的段落
        lines = job_desc.split('\n')
        plus_lines = []
        for line in lines:
            if any(keyword in line for keyword in self.plus_skills_keywords):
                plus_lines.append(line.strip())
        
        return '\n'.join(plus_lines) if plus_lines else ''
    
    def score_responsibility(self, responsibility: str) -> float:
        """
        评分岗位职责（0-10分）
        
        评分标准：
        - 条目数量（按条目数评分，而不是文本长度）
        - 关键词匹配度（是否包含重要职责关键词）
        """
        if not responsibility:
            return 0.0
        
        # 将文本拆分成条目
        bullets = self.to_bullets(responsibility)
        bullet_count = len(bullets)
        
        score = 0.0
        
        # 条目数量评分（6分）- 条目越多说明职责越清晰
        if bullet_count >= 5:
            score += 6.0
        elif bullet_count >= 3:
            score += 4.0
        elif bullet_count >= 2:
            score += 2.5
        elif bullet_count >= 1:
            score += 1.0
        
        # 关键词匹配度（4分）
        keyword_count = sum(1 for keyword in self.responsibility_keywords if keyword in responsibility)
        score += min(keyword_count * 0.5, 4.0)
        
        return min(score, 10.0)
    
    def score_tech_stack(self, tech_stack: str) -> float:
        """
        评分技术栈（0-10分）
        
        评分标准：
        - 技术栈数量
        - 技术栈相关性（AI/ML相关技术权重更高）
        """
        if not tech_stack:
            return 0.0
        
        techs = [t.strip() for t in tech_stack.split(',')]
        score = 0.0
        
        # 技术栈数量（5分）
        score += min(len(techs) * 1.0, 5.0)
        
        # AI/ML相关技术权重（5分）
        ai_keywords = ['AI/ML', 'Python', '数据库']
        ai_count = sum(1 for tech in techs if tech in ai_keywords)
        score += min(ai_count * 1.5, 5.0)
        
        return min(score, 10.0)
    
    def score_required_skills(self, required_skills: str) -> float:
        """
        评分必备技能（0-10分）
        
        评分标准：
        - 条目数量（按条目数评分，而不是文本长度）
        - 技能明确性
        """
        if not required_skills:
            return 0.0
        
        # 将文本拆分成条目
        bullets = self.to_bullets(required_skills)
        bullet_count = len(bullets)
        
        score = 0.0
        
        # 条目数量评分（6分）- 条目越多说明技能要求越清晰
        if bullet_count >= 5:
            score += 6.0
        elif bullet_count >= 3:
            score += 4.0
        elif bullet_count >= 2:
            score += 2.5
        elif bullet_count >= 1:
            score += 1.0
        
        # 技能明确性（4分）
        keyword_count = sum(1 for keyword in self.required_skills_keywords if keyword in required_skills)
        score += min(keyword_count * 0.8, 4.0)
        
        return min(score, 10.0)
    
    def score_plus_skills(self, plus_skills: str) -> float:
        """
        评分加分技能（0-10分）
        
        评分标准：
        - 是否有加分项
        - 加分项的价值
        """
        if not plus_skills:
            return 5.0  # 没有加分项不算扣分，给中等分数
        
        score = 5.0
        
        # 加分项存在（3分）
        score += 3.0
        
        # 加分项价值（2分）
        if len(plus_skills) > 30:
            score += 2.0
        elif len(plus_skills) > 10:
            score += 1.0
        
        return min(score, 10.0)
    
    def analyze_job(self, job_desc: str) -> Dict:
        """
        分析单个岗位
        
        Args:
            job_desc: 岗位描述文本
        
        Returns:
            分析结果字典
        """
        # 提取4个维度
        responsibility = self.extract_job_responsibilities(job_desc)
        tech_stack_categories, tech_stack_details = self.extract_tech_stack(job_desc)
        required_skills = self.extract_required_skills(job_desc)
        plus_skills = self.extract_plus_skills(job_desc)
        
        # 评分
        responsibility_score = self.score_responsibility(responsibility)
        tech_stack_score = self.score_tech_stack(tech_stack_categories)
        required_skills_score = self.score_required_skills(required_skills)
        plus_skills_score = self.score_plus_skills(plus_skills)
        
        # 综合评分（加权平均）
        # 权重：岗位职责40%，技术栈10%，必备技能40%，加分技能10%
        # 岗位职责和必备技能占主要权重，因为它们包含最核心的信息
        total_score = (
            responsibility_score * 0.4 +
            tech_stack_score * 0.1 +
            required_skills_score * 0.4 +
            plus_skills_score * 0.1
        )
        
        return {
            '岗位职责': responsibility,
            '技术栈': tech_stack_categories,  # 保留类别字段
            '技术栈明细': tech_stack_details,  # 新增明细字段
            '必备技能': required_skills,
            '加分技能': plus_skills,
            '职责评分': round(responsibility_score, 2),
            '技术栈评分': round(tech_stack_score, 2),
            '必备技能评分': round(required_skills_score, 2),
            '加分技能评分': round(plus_skills_score, 2),
            '综合评分': round(total_score, 2),
        }
    
    def analyze_excel(self, excel_path: str, output_path: str = None) -> pd.DataFrame:
        """
        分析Excel文件中的所有岗位
        
        Args:
            excel_path: Excel文件路径
            output_path: 输出文件路径（可选，默认在原文件名后加_analyzed）
        
        Returns:
            分析后的DataFrame
        """
        logger.info(f"开始分析Excel文件: {excel_path}")
        
        # 读取Excel
        df = pd.read_excel(excel_path)
        logger.info(f"读取到 {len(df)} 条岗位数据")
        
        # 检查是否有职位描述列
        if '职位描述' not in df.columns:
            logger.error("Excel文件中没有找到'职位描述'列")
            return df
        
        # 分析每条岗位
        results = []
        for idx, row in df.iterrows():
            job_desc = str(row.get('职位描述', ''))
            analysis = self.analyze_job(job_desc)
            
            # 合并原始数据和分析结果
            result_row = row.to_dict()
            result_row.update(analysis)
            results.append(result_row)
            
            if (idx + 1) % 10 == 0:
                logger.info(f"已分析 {idx + 1}/{len(df)} 条岗位")
        
        # 创建新的DataFrame
        result_df = pd.DataFrame(results)
        
        # 按综合评分排序
        result_df = result_df.sort_values('综合评分', ascending=False).reset_index(drop=True)
        
        # 保存结果（添加时间戳避免重复）
        if not output_path:
            base_name = os.path.splitext(excel_path)[0]
            # 移除可能已存在的 _analyzed 后缀
            if base_name.endswith('_analyzed'):
                base_name = base_name[:-9]
            # 添加时间戳
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"{base_name}_analyzed_{timestamp}.xlsx"
        
        # 如果文件已存在且被占用，尝试添加序号
        if os.path.exists(output_path):
            try:
                # 尝试打开文件，检查是否被占用
                with open(output_path, 'r+b'):
                    pass
            except PermissionError:
                # 文件被占用，添加序号
                base_name = os.path.splitext(output_path)[0]
                counter = 1
                while os.path.exists(f"{base_name}_{counter}.xlsx"):
                    counter += 1
                output_path = f"{base_name}_{counter}.xlsx"
                logger.warning(f"⚠️ 原文件被占用，使用新文件名: {output_path}")
        
        result_df.to_excel(output_path, index=False)
        file_path = os.path.abspath(output_path)
        logger.info(f"分析完成！结果已保存到: {output_path}")
        logger.info(f"📁 完整路径: {file_path}")
        logger.info(f"综合评分范围: {result_df['综合评分'].min():.2f} - {result_df['综合评分'].max():.2f}")
        print(f"\n📁 文件保存位置: {file_path}")
        
        return result_df


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("使用方法: python job_analyzer.py <Excel文件路径> [输出文件路径]")
        print("示例: python job_analyzer.py boss_研发AI产品经理_20260128_212443.xlsx")
        sys.exit(1)
    
    excel_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    
    analyzer = JobAnalyzer()
    result_df = analyzer.analyze_excel(excel_path, output_path)
    
    # 显示前10名
    print("\n" + "=" * 60)
    print("综合评分前10名岗位：")
    print("=" * 60)
    top_10 = result_df.head(10)
    for idx, row in top_10.iterrows():
        print(f"\n{idx + 1}. {row.get('岗位名称', '未知')}")
        print(f"   综合评分: {row['综合评分']:.2f}")
        print(f"   职责评分: {row['职责评分']:.2f} | 技术栈评分: {row['技术栈评分']:.2f}")
        print(f"   必备技能评分: {row['必备技能评分']:.2f} | 加分技能评分: {row['加分技能评分']:.2f}")
