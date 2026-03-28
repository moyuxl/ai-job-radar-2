# Navi AI — Web 操作台使用说明

> 项目总览与文档导航见根目录 [**README.md**](README.md)。

## 功能概述

Navi AI 是一个本地 Web 操作台，用于 Boss 直聘职位采集、分析、赛道筛选、简历匹配与差距分析。支持：

- ✅ 职位抓取（列表 + 可选详情，数据入库 SQLite）
- ✅ LLM 语义过滤（剔除与搜索目标不相关的岗位）
- ✅ 深度分析（结构化抽取 + 评分）
- ✅ 赛道标注（公司属性、岗位实质、岗位方向）
- ✅ 简历解析（PDF 提取 profile + 求职偏好）
- ✅ 匹配评分（硬筛 + 粗评批量 + 高分岗位深度 Match Agent）
- ✅ 改写 Agent（差距分析、改写建议、投递推荐）
- ✅ 实时进度、日志、Token 统计
- ✅ 结果导出 Excel

## 启动方式

```bash
python start_web_console.py
```

或：

```bash
uvicorn web_console:app --host 0.0.0.0 --port 8001 --no-access-log
```

（`--no-access-log` 可选，用于减少状态轮询产生的访问日志。）

访问：

- **Landing Page（营销首页）：** http://localhost:8001/（支持 **中文 / English**，右上角滑块切换，默认中文，语言偏好存 `localStorage`）。页面结构：**宏观洞察**（Hero + 雷达动画与悬浮卡）→ **深度匹配**（三列能力）→ **自动化执行**（步骤说明 + SQLite 示意）→ 底部 CTA；顶栏锚点与滚动同步高亮。
- **工作台（原操作台）：** http://localhost:8001/workbench  

从落地页主 CTA **探索你的职位地图 / Explore Your Job Map**、**进入工作台 / Open Workbench**、**开始使用** 等链接进入 `/workbench`。

工作台页面为 **Dashboard 布局**：顶栏仅 **Dashboard** 与占位用户标签（无额外进度条）；左侧 **Workflow**（6 步，加宽字号略放大）；中间 **大标题为当前任务名称**（如「职位抓取」），副标题为说明；**任务状态与日志**块会出现在**当前步骤表单正下方**（切换步骤时一起移动），其中 **预计完成时间** 为估算值（以日志「总用时」为准）；底部 **上一步 / 下一步**。结果文件仍在主内容下方辅助区。

## 界面设计规范

前端视觉遵循 **Architectural Navigator** 设计系统，详见仓库内 [`docs/DESIGN.md`](docs/DESIGN.md)（色板、字体 Manrope + Inter、Surface 层级、无硬线分隔、主按钮蓝渐变等）。

### 前端文件

| 文件 | 作用 |
|------|------|
| `templates/landing.html` | 落地页 `/` |
| `templates/web_console.html` | 工作台 `/workbench` |
| `static/styles/console.css` | 表单、可折叠区块、`#crawlSection` 职位抓取等 |
| `static/styles/workbench.css` | 顶栏、侧栏 Workflow、步骤大标题/说明、主内容区布局 |

### 工作台 · 字号层级（`html` 默认 16px 时）

便于与设计稿对照；实现见 `console.css` 的 `:root` 变量（`--font-wb-crawl-*`）与 `workbench.css` 的步骤标题。

| 层级 | 约 rem | 约 px | 说明 |
|------|--------|--------|------|
| 步骤主标题（如「职位抓取」） | 1.3125rem | 21px | 约为正文 1.5 倍 |
| 表单项标签（城市、学历等） | 0.95rem | 15.2px | 与主按钮「开始抓取」同档 |
| 主按钮 `.btn` | 0.95rem | 15.2px | 全局一致 |
| 表单正文（下拉、输入、经验多选、勾选说明、预设 chip） | 0.875rem | 14px | `#crawlSection` 内与经验复选行对齐 |
| 步骤说明 `.workbench-step-desc` | 0.875rem | 14px | 与表单正文同档 |
| 辅助 `.form-hint`（抓取区） | 0.8125rem | 13px | 略小于正文 |
| 步骤徽章「步骤 n/6」 | 0.75rem | 12px | 元信息 |

## 使用流程

### 1. 职位抓取

- **关键词**：必填，如「AI产品经理」「Python开发」
- **城市 / 学历 / 经验 / 薪资**：下拉选择，与 Boss 直聘筛选一致
- **工作经验**：可多选（如 1-3年 + 3-5年）
- **最大页数**：默认 1
- **爬取详情页**：勾选后爬取职位描述
- **LLM 语义过滤**：勾选后剔除与搜索目标不相关的岗位

抓取结果写入 **jobs.db**，岗位列表与详情持久化存储。

### 2. 深度分析

- 选择数据来源（最近抓取或指定 Excel 路径）
- 选择分析模型（DeepSeek Chat / Reasoner）
- 点击「开始分析」→ 逐条 LLM 抽取 + 评分 → 生成带评分 Excel

### 3. 赛道标注

- 筛选条件：公司属性、岗位实质、岗位方向
- 支持关键词快捷筛选（`jobs.source_keyword`，含已抓取但未标注赛道的岗位）
- 点击「开始赛道标注」→ LLM 逐条标注 → 结果入库
- **预计完成时间**：按实测约 75 条共 2 分钟（均摊 `120/75` 秒/条）估算；以日志「总用时」为准
- 可导出筛选后的 Excel（带时间戳）

### 4. 简历解析

- **上传 PDF**：解析为结构化 profile（技能、经历、亮点等）
- **求职偏好**：薪资、城市、公司类型、赛道偏好、排除项等
- 支持「读取本地」已有 profile JSON，支持「更改求职偏好」

### 5. 匹配评分（三层漏斗）

- **搜索关键词**：「开始匹配」旁下拉与赛道标注区快捷标签同源，数据来自 `GET /api/options/source-keywords`（`jobs.source_keyword`，即抓取时使用的关键词）。选「全部」则不按关键词过滤；若同时点了赛道区的关键词标签，且下拉里也选了词，**以匹配区下拉为准**。
- **列表范围**：匹配结果列表**仅展示符合当前赛道勾选与搜索关键词的岗位**（前端按 `get_match_results` 结果过滤），避免混入其它关键词/赛道下曾评分的记录；调整筛选或切换简历后会刷新。
- **预计完成时间**：按实测约 **69 条共 9 分钟**（均摊 `540/69` 秒/条，含粗评与深度匹配等整段；以日志「总用时」为准）
- **Layer 0**：硬筛（薪资区间、城市、公司类型黑名单），无 LLM。
- **Layer 1**：轻量批量四维评分（`match_analyzer` 批量调用），快速省 token。
- **Layer 2**：仅当粗评 **match_score ≥ 80**（`HEAVY_THRESHOLD`）时，对该岗位再跑 **深度 Match Agent**（默认 **单次** `submit_match_result` + 代码层后置校验，不通过则最多重试 2 次；可选 `MATCH_AGENT_MODE=loop` 恢复多轮工具循环），结果写入 `match_results.gap_analysis_json`，`agent_version` 为 `match_agent_v1`；仅粗评则为 `coarse_v1`。`coarse_score` 记录粗分层分数便于对照。
- 实现位置：**深度匹配**在 `match_agent.py`（入口 `run_match_agent`；单次 `run_match_agent_single`；多轮 `run_match_agent_loop`）；**批量/单条任务调度**在 `match_service.py`（含单岗位 **重新匹配** `run_rerun_match_one`，API：`POST /api/match/rerun_one`）。**loop 模式**下每一轮「模型 + 工具」之后会按 `MATCH_AGENT_ROUND_DELAY_SEC`（默认 1s）暂停；发往 LLM 的请求会对较早的 `tool` 返回做压缩（保留最近 `MATCH_AGENT_TOOL_COMPRESS_KEEP_LAST` 条全文）；若 **`verify_score` 已通过** 但后续 **submit 断流/未调用**，会用缓存的 **verify draft** 规范化为最终结果（写入 `gap_analysis_json` 时可能含 `_meta_fallback`），避免整段深度分析白跑。
- 结果按 match_score 降序；有深度分析时列表中会显示 **投递建议** 小标签（四档）。标签右侧为 **投递标记**（`match_results.applied`：**0** 未标记、**1** 已投 ✓、**2** 不投 ✗）：**左键**切换已投/取消（从未标记或「不投」进入已投），**右键**标记为不投（叉）；重新跑匹配会保留该字段。每条结果可 **重新匹配**，对该岗位单独重跑粗评+深度 Agent（覆盖原记录）。
- 点击 **开始匹配** 时，会先 **自动扫一轮** 已有 `match_results`：**粗评 ≥80 但仍非 `match_agent_v1` / 无有效 `gap_analysis_json`** 的岗位会按序 **补跑深度匹配**（单次任务最多 `MATCH_DEEP_BACKFILL_MAX` 条，默认 50）；日志中会统计 **粗评低于 80 仅粗评** 与 **≥80 仍缺深度** 的数量（低于 80 不跑深度属正常）。
- 已匹配过的岗位在「新岗粗评」阶段会跳过，节省 Token；但上述 **深度补跑** 会更新旧记录。
- **共性分析**（可选）：在匹配区点击「生成共性报告」，**与「开始匹配」同一套 `track_params`**（公司属性 / 岗位实质 / 方向 / 置信度 / **当前搜索关键词**），仅在该批岗位对应的 **深度匹配 `match_agent_v1`、且已有有效 `gap_analysis_json`** 记录中取样（与 `get_jobs_by_track` 对齐，见 `db._sql_track_conditions_on_jobs_alias`）。优先按 `match_score` 取 **≥ HEAVY_THRESHOLD（默认 80）**；若高分深度匹配 **不足 5 条**，则按分数**向下**再纳入**低于 80 分**的深度匹配，直至至少 **5 条**（仍不足则无法生成）；参与条数 **至多 10 条**（`db.COMMONALITY_MIN_JOBS` / `get_top_deep_matches_for_commonality`）。不传 `track_params` 时与历史行为一致（不按赛道过滤整份简历下深度匹配）。仅传截断后的 gaps / 隐含期望 / ATS / 四维 + `profile`，不传 JD。**成功结果写入本地** `output/commonality/{stem}.commonality_report[.tp_{哈希}].json`（按赛道参数区分；无 `track_params` 时仍为 `{stem}.commonality_report.json`；旧版同目录简历旁缓存仍可读取）。**按钮逻辑**：无本地缓存时先 `GET /api/match/commonality_report/cached?track_params=...`（与 POST 同参），有则直接展示；无则 `POST` 生成并落盘；**当前已展示过报告**（无论来自本地文件还是刚 LLM 生成）再次点击则 **POST 重新生成** 并覆盖对应哈希文件。实现见 `commonality_analysis.py`、`db.get_top_deep_matches_for_commonality`。

### 6. 改写 Agent（主简历改写）

- **推荐流程**：匹配评分 → **生成共性报告**（在当前关键词/赛道筛选范围内，≥80 分的深度匹配里按分取 Top10，可少于 10）→ **基于共性优化主简历**（一次改写，覆盖这批岗共性；无需逐岗改 10 版）。
- 改写调用 `gap_agent.run_agent2_master_from_commonality`，结果写入 `agent_analysis`，**`job_id` 固定为 `__COMMONALITY_MASTER__`**（与单岗记录区分）。API：`POST /api/gap/master_rewrite`，查询：`GET /api/gap/master_result?resume_path=...`。
- **差距数据**：来自共性报告中的 `priority_gaps` / `resume_optimizations`（展示用），**不再按单岗拉 JD**。
- **旧接口**：`POST /api/gap/start` 仍为「单岗 + 该岗 JD」改写，保留兼容；主流程已改为共性驱动主简历。
- 主简历整体预期（四档，基于改后四维）：**投递** / **谨慎投递** / **可以试但概率低** / **不建议投递**

## 数据存储

- **jobs.db**：SQLite 数据库
  - `jobs`：岗位列表 + 详情
  - `analysis_results`：分析结果
  - `match_results`：匹配评分
  - `agent_analysis`：差距分析结果
- **output/**：Excel 导出文件
- **resumes/**：简历 JSON（profile + preferences）

## 配置

### .env

```
# DeepSeek（分析、赛道、匹配、差距分析）
DEEPSEEK_API_KEY=your_key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL_CHAT=deepseek-chat
DEEPSEEK_MODEL_REASONER=deepseek-reasoner
```

## 主要 API

| 端点 | 说明 |
|------|------|
| POST /api/crawl/start | 启动抓取 |
| GET /api/options/cities 等 | 城市、学历、经验、薪资、模型等下拉选项 |
| POST /api/analysis/start | 启动分析 |
| POST /api/track-label/start | 启动赛道标注 |
| POST /api/jobs/by-track、POST /api/jobs/export | 按赛道筛选与导出 |
| POST /api/resume/upload、POST /api/resume/load 等 | 简历上传/列表/偏好 |
| POST /api/match/start | 启动匹配 |
| POST /api/match/rerun_one | 单岗位重新匹配 |
| GET /api/match/results | 匹配结果列表 |
| POST /api/match/applied | 更新投递标记（`applied_status`: 0/1/2；兼容旧字段 `applied` 布尔） |
| GET/POST /api/match/commonality_report* | 共性报告缓存与生成 |
| POST /api/gap/start | 单岗差距/改写（兼容） |
| POST /api/gap/master_rewrite、GET /api/gap/master_result | 基于共性的主简历改写 |
| GET /api/gap/result | 获取单岗差距分析缓存 |
| GET /api/task/{id}/status、POST /api/task/{id}/confirm | 任务状态与登录等确认 |
| GET /api/file/{path} | 受控路径下的结果文件预览 |

## 文件结构

```
ai-job-radar2/
├── web_console.py          # FastAPI 入口（/、/workbench、/api/*）
├── start_web_console.py    # 启动脚本
├── task_manager.py         # 任务管理
├── db.py                   # SQLite 封装
├── crawler_service.py      # 抓取服务
├── analysis_service.py     # 分析服务
├── track_label_service.py  # 赛道标注服务
├── match_service.py        # 匹配服务
├── match_agent.py          # 深度 Match Agent
├── gap_service.py          # 差距分析服务
├── gap_agent.py            # 改写 Agent + 从 match_results 构造差距上下文
├── resume_extractor.py     # 简历解析
├── match_analyzer.py       # 匹配评分逻辑
├── track_labeler.py        # 赛道 LLM 标注
├── templates/
│   ├── landing.html
│   └── web_console.html
├── static/styles/
│   ├── console.css
│   └── workbench.css
├── docs/DESIGN.md          # 设计系统
├── output/                 # Excel 输出
└── jobs.db                 # 数据库（自动创建）
```

## 注意事项

1. **浏览器窗口**：抓取会打开 DrissionPage 浏览器，请勿关闭
2. **登录确认**：若检测到登录页，点击「我已登录，继续执行」
3. **任务状态**：轮询展示，完成后停止刷新
4. **匹配缓存**：同一简历+岗位只评一次，差距分析同理
