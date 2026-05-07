"""DeepSeek：发往 API 的 model 名称与环境变量（兼容旧变量名）。"""
import os


def deepseek_flash_api_model() -> str:
    return (
        os.getenv("DEEPSEEK_MODEL_V4_FLASH")
        or os.getenv("DEEPSEEK_MODEL_CHAT")
        or "deepseek-v4-flash"
    )


def deepseek_pro_api_model() -> str:
    return (
        os.getenv("DEEPSEEK_MODEL_V4_PRO")
        or os.getenv("DEEPSEEK_MODEL_REASONER")
        or "deepseek-v4-pro"
    )
