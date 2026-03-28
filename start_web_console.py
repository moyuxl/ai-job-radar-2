"""
启动 Web 操作台

本地：不设环境变量时仍为 http://localhost:8001（与以前一致）。
云上：平台若注入 PORT（如 Railway / Render），则自动使用该端口。
"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8001"))
    print("=" * 60)
    print("AI 职位雷达 - Web 操作台")
    print("=" * 60)
    print("\n服务器启动中...")
    print(f"Landing: http://localhost:{port}")
    print(f"工作台: http://localhost:{port}/workbench")
    print("\n按 Ctrl+C 停止服务器")
    print("已启用自动重载：修改代码后会自动重新加载\n")

    # 使用导入字符串以支持 reload 功能
    # access_log=False：不打印每条 HTTP 访问日志（避免状态轮询刷屏）
    uvicorn.run(
        "web_console:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        access_log=False,
    )
