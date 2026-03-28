"""
Boss 直聘工作经验代码映射
⚠️ 注意：这些代码是从Boss直聘实际API中获取的
"""
EXPERIENCE_CODE_MAP = {
    '': '不限',
    '101': '经验不限',
    '103': '1年以内',
    '104': '1-3年',
    '105': '3-5年',
}

# 反向映射：经验名称 -> 经验代码
# 注意：'不限' 和 '不限（显示全部）' 对应空字符串，表示不传 experience 参数
EXPERIENCE_NAME_TO_CODE = {
    '不限经验': '101',
    '经验不限': '101',
    '不限': '',           # 不筛选 = 空字符串
    '不限（显示全部）': '',
    '无要求': '101',
    '1年以内': '103',
    '1年以下': '103',
    '1-3年': '104',
    '3-5年': '105',
}

# 常用经验列表（用于用户选择）
# 注意区分：
#   '' (空) = 不限：不传 experience 参数，显示所有经验要求的岗位
#   '101' = 经验不限：只显示「经验不限」的岗位
COMMON_EXPERIENCES = [
    ('', '不限'),      # 不传 experience，所有年限都出现
    ('101', '经验不限'),          # experience=101，只显示经验不限的岗位
    ('103', '1年以内'),
    ('104', '1-3年'),
    ('105', '3-5年'),
]


def get_experience_code(experience_name: str) -> str:
    """
    根据经验名称获取经验代码
    
    Args:
        experience_name: 经验名称（如：1-3年、不限经验）
    
    Returns:
        经验代码（如：104），如果未找到返回 '101'（不限经验）
    """
    return EXPERIENCE_NAME_TO_CODE.get(experience_name, '101')


def get_experience_name(experience_code: str) -> str:
    """
    根据经验代码获取经验名称
    
    Args:
        experience_code: 经验代码（如：104）
    
    Returns:
        经验名称（如：1-3年），如果未找到返回代码本身
    """
    return EXPERIENCE_CODE_MAP.get(str(experience_code), str(experience_code))
