# AI 职位雷达 v2 — 项目进度与复盘文档

本文档用于**复盘、续写与交接**：总结当前进度、架构、遇到的问题与解决方案，方便自己复盘或在新对话中直接粘贴给 AI 续写。

---

## 一、项目目标与成果

### 1.1 产品定位

- **产品名**：Navi AI — 点亮你的求职地图（原 AI Job Strategic Radar）
- **目标用户**：转行/求职 AI 相关岗位的人
- **核心价值**：本地 Web 操作台 → 抓取 Boss 直聘 → 入库 SQLite → LLM 分析/赛道标注/匹配评分/差距分析 → 结构化结果与投递建议

### 1.2 已实现功能（验收通过）

| 模块 | 功能 | 说明 |
|------|------|------|
| **职位抓取** | 列表 + 详情 | DrissionPage 监听 joblist.json，数据写入 jobs.db |
| **LLM 语义过滤** | 可选 | 列表爬取后剔除与搜索目标不相关的岗位 |
| **深度分析** | 抽取 + 评分 | 逐条 LLM 抽取结构化字段，计算 completeness/actionability，写回 DB |
| **赛道标注** | 公司属性、岗位实质、岗位方向 | LLM 逐条标注，支持筛选、导出 Excel |
| **简历解析** | PDF → profile | LLM 提取技能、经历、亮点等，支持求职偏好表单 |
| **匹配评分** | 硬筛 + 四维 LLM | 技能/经验/成长/契合，已匹配岗位跳过，结果入库 |
| **改写 Agent** | 改写建议 + 评估 | 差距来自 match_results 已存 JSON；仅调用改写 Agent（submit_rewrite_result），投递建议四档 |
| **实时状态** | 进度、日志、Token | 各任务支持阶段名、滚动日志、Token 统计 |
| **可折叠模块** | UI | 各主模块支持折叠 |
| **工作台布局** | Dashboard | 顶栏、左侧 Workflow（6 步）、步骤大标题与说明、任务状态随当前步骤、底部上一步/下一步；样式见 `workbench.css` + `console.css` |
| **投递建议** | 四档 | 基于改后总分（投递/谨慎/可以试但概率低/不建议） |
| **改写来源** | 原文改写 / 合理延伸 | 每条改写标注依据，用户自行判断是否采用 |

---

## 二、架构与数据流

### 2.1 整体流程

```
用户打开 http://localhost:8001/（落地页）或 http://localhost:8001/workbench（工作台）
    ↓
[抓取] 关键词/城市/学历等 → 开始抓取
    → 列表爬取 → 可选 LLM 语义过滤 → 可选详情爬取
    → 数据写入 jobs.db（jobs 表）
    ↓
[分析] 选择数据源 → 开始分析
    → 从 DB 或 Excel 读岗位 → 逐条 LLM 抽取+评分
    → 写回 analysis_results，可选导出 Excel
    ↓
[赛道标注] 筛选条件 → 开始标注
    → 从 DB 取待标注岗位 → LLM 逐条标注（公司属性、岗位实质、岗位方向）
    → 更新 jobs 表
    ↓
[简历] 上传 PDF / 读取本地 → 解析 profile + 填偏好
    → 保存 resumes/*.json
    ↓
[匹配] 选简历 + 硬筛条件 → 开始匹配
    → 硬筛（薪资/城市/公司类型）→ 对未匹配岗位逐条 LLM 四维评分
    → 写入 match_results
    ↓
[差距分析] 匹配结果点「差距分析」
    → 须有 match_results：读 gap_analysis_json（深度）或粗评 gaps 合成差距上下文
    → 改写 Agent：submit_rewrite_result（原文改写/合理延伸、改前改后四维）
    → 计算投递建议，写入 agent_analysis
```

### 2.2 数据库表结构（jobs.db）

| 表 | 说明 |
|----|------|
| jobs | 岗位列表 + job_desc、赛道字段（company_type、job_nature、job_direction_primary 等） |
| analysis_results | 分析结果（work_content、must_have_skills、scores 等） |
| match_results | 简历+岗位匹配（match_score、四维、strengths、gaps、advice） |
| agent_analysis | 差距分析（years_verdict、gap_items、materials、rewrites、eval_before/after、recommendation） |

### 2.3 技术选型

| 模块 | 技术 | 说明 |
|------|------|------|
| 存储 | SQLite（jobs.db） | 岗位、分析、匹配、差距分析持久化 |
| 爬虫 | DrissionPage | 监听 joblist.json，不直接调 Boss API |
| Web | FastAPI | 单端口 8001，页面 + 各类 API |
| 任务 | TaskManager | 内存任务状态、进度、日志 |
| LLM | OpenAI 兼容 API | DeepSeek，分析/赛道/匹配/差距分析 |
| 差距分析 | Function Calling | 仅改写 Agent 一个 tool（submit_rewrite_result）；差距数据来自 DB JSON |

---

## 三、关键文件与职责

| 文件 | 职责 |
|------|------|
| **web_console.py** | FastAPI 入口；`/`、`/workbench`；/api/crawl/start、/api/analysis/start、/api/track-label/start、/api/match/start、/api/gap/*、/api/task/{id}/status 等 |
| **db.py** | SQLite 封装：init_db、upsert_job、get_jobs_by_track、save_match_result、get_agent_analysis、save_agent_analysis 等 |
| **crawler_service.py** | 抓取封装：ZhipinCrawler、登录确认、写 DB |
| **analysis_service.py** | 分析：读 DB/Excel、LLM 抽取、写 analysis_results |
| **track_label_service.py** | 赛道标注：LLM 逐条、更新 jobs |
| **match_service.py** | 匹配：硬筛、get_matched_job_ids 跳过已评、LLM 四维、写 match_results |
| **gap_service.py** | 差距分析：get_match_result_row → build_rewrite_context_from_match_row → run_agent2、save_agent_analysis |
| **gap_agent.py** | 改写 Agent（AGENT2_TOOLS）；从 match_results 构造 gap_context |
| **match_analyzer.py** | 硬筛逻辑、LLM 四维评分、JSON 解析 |
| **resume_extractor.py** | PDF 解析、LLM 提取 profile、load_resume_json |
| **track_labeler.py** | 赛道 LLM prompt、公司属性/岗位实质/岗位方向 |
| **task_manager.py** | create_task、update_status/progress/result、add_log、wait_for_confirm |

---

## 四、已遇到的问题与解决情况

| 问题 | 原因/现象 | 解决状态 |
|------|-----------|----------|
| 点「确认」后任务不继续 | 登录确认逻辑在 crawl_jobs 内部，Web 模式误用 input() | 已修复：task_id 时走 wait_for_confirm |
| 分析结果 work_content/必备技能 为空 | LLM 输出 JSON 解析失败或格式不稳定 | 已做：aggressive_repair 重试；仍失败写 error 列 |
| JSONDecodeError: Extra data | LLM 返回多个拼接的 JSON 对象 | 已修复：_extract_json_array_from_text 迭代解析 |
| AttributeError: set_result | match_service 误用 set_result | 已修复：改用 update_result |
| 匹配重复调用 LLM | 同一简历+岗位多次评分浪费 Token | 已修复：get_matched_job_ids 跳过已评 |
| 年限硬伤仍生成改写 | Agent 2 未按条件跳过 | 已修复：system prompt 加「年限硬伤不生成改写」 |
| 投递建议缺失 | 旧缓存无 recommendation | 已修复：get_agent_analysis 中按 eval_after 计算兼容 |

---

## 五、已知风险与待观察

1. **LLM API 连接不稳定**  
   重试 + 普通模式兜底；持续失败需检查 .env、网络、限流。

2. **登录检测依赖页面关键词**  
   Boss 改版可能误判，当前已加「已登录」判断。

3. **任务仅内存**  
   TaskManager 不落库，重启后任务列表清空；差距分析/匹配结果已持久化到 DB。

4. **改写「合理延伸」边界**  
   依赖 LLM 理解「合理具备」，可能过度延伸，需用户自行判断。

---

## 六、本地运行与自测

### 6.1 环境

- Python 3.8+（推荐 3.10）
- `pip install -r requirements.txt`

### 6.2 启动

```bash
python start_web_console.py
```

访问：**http://localhost:8001/**（落地页）或 **http://localhost:8001/workbench**（工作台）

### 6.3 配置

- **.env**：DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL、DEEPSEEK_MODEL_CHAT/REASONER
- **jobs.db**：自动创建于项目根目录
- **output/**：Excel 导出
- **resumes/**：简历 JSON

### 6.4 建议自测流程

1. 抓取：关键词 + 城市，最大页数 1 → 开始抓取 → 登录确认（若有）→ 完成
2. 分析：选数据源 → 开始分析 → 看进度与 Token → 完成
3. 赛道：选筛选条件 → 开始标注 → 导出 Excel
4. 简历：上传 PDF → 填偏好 → 保存
5. 匹配：选简历 → 开始匹配 → 看结果列表
6. 差距分析：点某岗位「差距分析」→ 看 Agent 进度 → 看投递建议与改写

---

## 七、建议的下一步（供复盘或新对话）

1. **JD 结构化**：借鉴 job-matcher，将 JD 拆为「核心职责」「必备/加分技能」「隐藏期望」「ATS 关键词」，提升差距分析精度。
2. **置信度**：四维评分加 Confidence（High/Medium/Low），区分信息不足与确实不匹配。
3. **面试问题**：差距分析结果中增加「可能面试问题」。
4. **任务持久化**：TaskManager 落库，刷新页面不丢任务列表。
5. **错误码文档**：README 补充常见错误与日志含义。

---

## 八、给「新对话」的简短提示词

```
本项目是 Navi AI（AI 职位雷达 v2）：本地 Web 操作台（FastAPI，端口 8001），抓取 Boss 直聘 + SQLite 存储 + LLM 分析/赛道标注/匹配评分/差距分析。

请先阅读 README.md 与 PROJECT_STATUS_AND_CONTINUATION.md。数据流：抓取→jobs.db→分析→赛道标注→简历解析→匹配→差距分析。入口页面：/ 落地页，/workbench 工作台。关键文件：db.py、gap_agent.py、match_analyzer.py、gap_service.py。差距分析仅改写 Agent；差距 JSON 来自 match_results（深度匹配或粗评合成）。
```

---

## 九、相关文档索引

- **README.md** — 项目入口、快速开始、仓库结构
- **README_WEB_CONSOLE.md** — Web 操作台使用说明（含字号层级、API 摘要）
- **docs/DESIGN.md** — 设计系统与工作台字号实现说明
- **README_API.md** — 职位描述深度分析 API（`api_server.py`，单机版，与 Web 控制台可并存）
- **.env** — API 与运行配置
- **.env.example** — 配置模板
