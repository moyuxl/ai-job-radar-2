# 职位描述深度分析 API

> **与主力产品的关系**：当前主线为 **Navi AI Web 工作台**（`web_console.py`，默认端口 **8001**），说明见根目录 [**README.md**](README.md) 与 [**README_WEB_CONSOLE.md**](README_WEB_CONSOLE.md)。  
> 本文档描述的是仓库内 **独立分析服务**（`api_server.py` / `start_api.py`，默认端口 **8000**），可与工作台并存，按需在本地选用。

使用 FastAPI 开发的职位描述深度分析服务，通过 LLM API 对 Excel 文件中的职位描述进行结构化分析和评分。

## 功能特点

- 📤 **文件上传**：支持通过 Web 界面或 API 上传 Excel 文件
- 🤖 **AI 分析**：使用 LLM 提取结构化字段（工作内容、必备技能、加分技能、信号词等）
- 📊 **智能评分**：自动计算信息完整度和可执行性评分
- 🎯 **自动排序**：按综合评分降序排列
- 📥 **结果下载**：生成带时间戳的分析结果 Excel 文件

## 安装依赖

```bash
pip install -r requirements.txt
```

## 配置

确保 `.env` 文件中包含以下配置：

```
SUPER_MIND_API_KEY=your_api_key
SUPER_MIND_BASE_URL=https://your-api-url.com/v1
SUPER_MIND_MODEL=your_model_name
```

## 启动服务器

### 方式一：使用启动脚本

```bash
python start_api.py
```

### 方式二：直接运行

```bash
python api_server.py
```

### 方式三：使用 uvicorn

```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

## 使用方式

### 1. Web 界面（推荐）

1. 启动服务器后，在浏览器中访问：`http://localhost:8000`
2. 点击上传区域或拖拽 Excel 文件
3. 点击"开始分析"按钮
4. 等待分析完成，下载结果文件

### 2. API 调用

#### 上传文件分析

```bash
curl -X POST "http://localhost:8000/analyze" \
  -F "file=@your_file.xlsx"
```

#### 分析指定路径的文件

```bash
curl "http://localhost:8000/analyze?file_path=D:/path/to/file.xlsx"
```

#### 下载结果文件

```bash
curl "http://localhost:8000/download?file_path=D:/path/to/result.xlsx" -o result.xlsx
```

### 3. API 文档

访问 `http://localhost:8000/docs` 查看交互式 API 文档（Swagger UI）

## 输出字段说明

分析结果 Excel 文件包含以下新增字段：

### 提取字段
- **工作内容**：JSON 格式，包含 task（任务）和 deliverable（交付物）
- **必备技能**：JSON 格式数组
- **加分技能**：JSON 格式数组
- **信号词-交付物**：JSON 格式数组
- **信号词-流程**：JSON 格式数组
- **信号词-指标**：JSON 格式数组
- **信号词-空话**：JSON 格式数组
- **证据片段**：JSON 格式数组，原文摘录

### 评分字段
- **信息完整度**：0-100 分（基于工作内容、技能要求的完整性）
- **可执行性**：0-30 分（基于交付物、流程、指标的提及）
- **综合评分**：0-130 分（信息完整度 + 可执行性）

### 标记字段
- **标记-信息不足**：布尔值，completeness < 45
- **标记-空话多**：布尔值，fluff_terms >= 4 且 actionability <= 10
- **标记-需人工审核**：布尔值，证据片段为空或工作内容/必备技能 < 2

### 其他
- **评分理由**：2-4 条短句，解释评分高低的原因

## 评分规则

### 信息完整度 (completeness) 0-100

- **工作内容条目数**：
  - >=5 条：40 分
  - 3-4 条：28 分
  - 1-2 条：16 分
  - 0 条：0 分

- **必备技能条目数**：
  - >=6 条：40 分
  - 4-5 条：28 分
  - 1-3 条：16 分
  - 0 条：0 分

- **加分技能条目数**：
  - >=3 条：20 分
  - 1-2 条：12 分
  - 0 条：4 分

### 可执行性 (actionability) 0-30

- 如果 deliverables（交付物）非空：+10 分
- 如果 process_terms（流程术语）非空：+10 分
- 如果 metrics_terms（指标术语）非空：+10 分

### 综合评分

```
total = completeness + actionability
```

## 注意事项

1. **Excel 文件要求**：必须包含"职位描述"列
2. **API 限制**：请根据你的 API 配额合理使用
3. **处理时间**：每个职位描述的分析时间取决于 API 响应速度，通常需要几秒到几十秒
4. **文件大小**：建议单次分析不超过 100 条职位描述
5. **错误处理**：如果某条分析失败，会在"分析状态"列中标记错误信息

## 故障排除

### 1. 无法连接到 API

- 检查 `.env` 文件中的 API 配置是否正确
- 确认网络连接正常
- 验证 API Key 是否有效

### 2. JSON 解析失败

- LLM 可能返回了非 JSON 格式的内容
- 检查日志中的错误信息
- 可以尝试降低 temperature 参数

### 3. 文件上传失败

- 确认文件格式为 .xlsx 或 .xls
- 检查文件是否包含"职位描述"列
- 查看服务器日志获取详细错误信息

## 技术栈

- **FastAPI**：Web 框架
- **OpenAI SDK**：LLM API 客户端（兼容自定义 API）
- **Pandas**：数据处理
- **OpenPyXL**：Excel 文件读写
- **Uvicorn**：ASGI 服务器
