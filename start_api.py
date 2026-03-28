"""
启动 API 服务器的便捷脚本
"""
import uvicorn
from api_server import app

if __name__ == "__main__":
    print("=" * 60)
    print("职位描述深度分析 API 服务器")
    print("=" * 60)
    print("\n服务器启动中...")
    print("访问地址: http://localhost:8000")
    print("API 文档: http://localhost:8000/docs")
    print("\n按 Ctrl+C 停止服务器\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
