"""
Boss 直聘城市代码映射
⚠️ 注意：这些代码是从Boss直聘实际API响应中获取的，不是手动编写的
"""
CITY_CODE_MAP = {
    # 用户从实际API中获取的城市代码
    '101020100': '上海',
    '101210100': '杭州',
    '101190400': '苏州',
    '101190200': '无锡',
    
    # 全国（默认）
    '100010000': '全国',
}

# 反向映射：城市名称 -> 城市代码
CITY_NAME_TO_CODE = {v: k for k, v in CITY_CODE_MAP.items()}

# 常用城市列表（用于用户选择）
COMMON_CITIES = [
    ('100010000', '全国'),
    ('101020100', '上海'),
    ('101210100', '杭州'),
    ('101190400', '苏州'),
    ('101190200', '无锡'),
]


def get_city_code(city_name: str) -> str:
    """
    根据城市名称获取城市代码
    
    Args:
        city_name: 城市名称（如：上海、杭州）
    
    Returns:
        城市代码（如：101020100），如果未找到返回 '100010000'（全国）
    """
    return CITY_NAME_TO_CODE.get(city_name, '100010000')


def get_city_name(city_code: str) -> str:
    """
    根据城市代码获取城市名称
    
    Args:
        city_code: 城市代码（如：101020100）
    
    Returns:
        城市名称（如：上海），如果未找到返回代码本身
    """
    return CITY_CODE_MAP.get(str(city_code), str(city_code))
