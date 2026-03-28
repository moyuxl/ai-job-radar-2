"""
Boss 直聘薪资代码映射
⚠️ 注意：这些代码是从Boss直聘实际API中获取的
"""
SALARY_CODE_MAP = {
    '402': '3k以下',
    '403': '3-5k',
    '404': '5-10k',
    '405': '10-20k',
    '406': '20-50k',
    '407': '50k以上',
}

# 反向映射：薪资名称 -> 薪资代码
SALARY_NAME_TO_CODE = {
    '3k以下': '402',
    '3-5k': '403',
    '5-10k': '404',
    '10-20k': '405',
    '20-50k': '406',
    '50k以上': '407',
    '不限': '',
    '不限薪资': '',
    '无要求': '',
}

# 常用薪资列表（用于用户选择）
COMMON_SALARIES = [
    ('', '不限薪资'),
    ('402', '3k以下'),
    ('403', '3-5k'),
    ('404', '5-10k'),
    ('405', '10-20k'),
    ('406', '20-50k'),
    ('407', '50k以上'),
]


def get_salary_code(salary_name: str) -> str:
    """
    根据薪资名称获取薪资代码
    
    Args:
        salary_name: 薪资名称（如：5-10k、不限薪资）
    
    Returns:
        薪资代码（如：404），如果未找到返回空字符串（不限）
    """
    return SALARY_NAME_TO_CODE.get(salary_name, '')


def get_salary_name(salary_code: str) -> str:
    """
    根据薪资代码获取薪资名称
    
    Args:
        salary_code: 薪资代码（如：404）
    
    Returns:
        薪资名称（如：5-10k），如果未找到返回代码本身
    """
    if not salary_code:
        return '不限薪资'
    return SALARY_CODE_MAP.get(str(salary_code), str(salary_code))
