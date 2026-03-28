"""
Boss 直聘学历代码映射
学历代码 -> 学历名称
⚠️ 注意：这些代码是从Boss直聘实际API获取的，不是手动编写的
数据来源：https://www.zhipin.com/wapi/zpgeek/search/job/condition.json
"""
# 从Boss直聘API获取的实际学历代码
DEGREE_CODE_MAP = {
    '0': '不限',
    '209': '初中及以下',
    '208': '中专/中技',
    '206': '高中',
    '202': '大专',
    '203': '本科',
    '204': '硕士',
    '205': '博士',
}

# 反向映射：学历名称 -> 学历代码
DEGREE_NAME_TO_CODE = {
    '不限': '0',
    '初中及以下': '209',
    '初中': '209',
    '中专/中技': '208',
    '中专': '208',
    '中技': '208',
    '高中': '206',
    '大专': '202',
    '本科': '203',
    '硕士': '204',
    '博士': '205',
    '学历不限': '0',
    '无要求': '0',
}

# 常用学历列表（用于用户选择）
COMMON_DEGREES = [
    ('0', '不限'),
    ('203', '本科'),
    ('204', '硕士'),
    ('205', '博士'),
    ('202', '大专'),
    ('206', '高中'),
]


def get_degree_code(degree_name: str) -> str:
    """
    根据学历名称获取学历代码
    
    Args:
        degree_name: 学历名称（如：本科、硕士）
    
    Returns:
        学历代码（如：203），如果未找到返回空字符串（不限）
    """
    return DEGREE_NAME_TO_CODE.get(degree_name, '')


def get_degree_name(degree_code: str) -> str:
    """
    根据学历代码获取学历名称
    
    Args:
        degree_code: 学历代码（如：203）
    
    Returns:
        学历名称（如：本科），如果未找到返回代码本身
    """
    if not degree_code:
        return '不限'
    return DEGREE_CODE_MAP.get(str(degree_code), str(degree_code))
