# Navi AI — 点亮你的求职地图

本地 Web 工作台：Boss 直聘职位采集 → SQLite 入库 → LLM 深度分析 / 赛道标注 / 简历匹配 / 差距与改写建议，全流程可在浏览器完成。

## 快速开始

```bash
pip install -r requirements.txt
# 配置 .env（见 README_WEB_CONSOLE.md「配置」）
python start_web_console.py
```

| 地址 | 说明 |
|------|------|
| http://localhost:8001/ | **Landing** 营销首页（中/英，语言存 `localStorage`） |
| http://localhost:8001/workbench | **工作台**（6 步 Workflow、抓取与分析等） |

## 文档索引

| 文档 | 内容 |
|------|------|
| [**README_WEB_CONSOLE.md**](README_WEB_CONSOLE.md) | 功能说明、启动方式、流程、API 摘要、注意事项 |
| [**docs/DESIGN.md**](docs/DESIGN.md) | Architectural Navigator 设计系统（色板、字体、组件） |
| [**PROJECT_STATUS_AND_CONTINUATION.md**](PROJECT_STATUS_AND_CONTINUATION.md) | 进度复盘、架构、问题与交接提示词 |
| [**README_API.md**](README_API.md) | 独立「职位描述深度分析」API（`api_server.py`，默认 8000 端口）；与当前主力工作台并存时可查阅 |
| [**docs/DEPLOY_PHASE_A.md**](docs/DEPLOY_PHASE_A.md) | **阶段 A**：Docker 上云与 HTTPS（**不替代**本地 `start_web_console.py`） |

## 技术栈（概要）

- **后端**：FastAPI（`web_console.py`）、SQLite（`jobs.db`）、DrissionPage 抓取
- **LLM**：OpenAI 兼容接口（默认 DeepSeek，见 `.env`）
- **前端**：Jinja 模板 + 静态资源；工作台布局见 `static/styles/workbench.css`，表单与控制台样式见 `static/styles/console.css`

## 仓库结构（核心）

```
ai-job-radar2/
├── web_console.py              # FastAPI 入口（页面 + /api/*）
├── start_web_console.py        # 启动脚本
├── task_manager.py             # 任务状态、日志、确认流
├── db.py                       # SQLite
├── crawler_service.py          # 抓取
├── analysis_service.py         # 深度分析
├── track_label_service.py      # 赛道标注
├── match_service.py            # 匹配（硬筛 + 粗评 + 深度 Match Agent）
├── match_agent.py              # 深度匹配 Agent
├── gap_service.py / gap_agent.py  # 改写与差距
├── resume_extractor.py         # 简历解析
├── templates/
│   ├── landing.html            # 首页
│   └── web_console.html        # 工作台
├── static/styles/
│   ├── console.css             # 表单、职位抓取区等
│   └── workbench.css           # Dashboard、侧栏、步骤标题
├── output/                     # Excel 导出
└── resumes/                    # 简历 JSON
```

## 许可证与声明

抓取与使用请遵守 Boss 直聘服务条款及当地法律法规；本工具仅供个人学习与求职辅助。
