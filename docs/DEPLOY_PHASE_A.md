# 阶段 A：上云与 HTTPS（不改变本地开发）

## 原则

- **本地开发**：继续在项目根目录执行 `python start_web_console.py`，默认仍是 **http://localhost:8001**（不设 `PORT` 时与以前一致）。
- **云上部署**：多一种跑法——用 **Docker 镜像** 在托管平台运行；**HTTPS 由平台在边缘自动提供**，应用内仍监听 HTTP。
- **阶段 A 不强制改业务代码**：不引入账号、不把 SQLite 换成云数据库；容器里会生成**新的空** `jobs.db`（首次请求前 `init_db` 已随应用加载执行）。

## 本地先验证 Docker（可选）

```bash
cd ai-job-radar2
docker build -t navi-web .
docker run --rm -p 8001:8001 --env-file .env navi-web
```

若构建时在 `apt-get install gcc` 或 Debian 源出现 **502 Bad Gateway**，多为镜像 CDN 瞬时故障：**直接重试** `docker build`，或当前 Dockerfile 已尽量**不装 gcc**（依赖用 wheel），避免依赖 `deb.debian.org`。

浏览器打开 http://localhost:8001 。  
（若本机没有 `.env`，可先去掉 `--env-file .env`，但 LLM 相关功能需要你在平台配置环境变量。）

## 云上托管（任选其一）

通用步骤：

1. 把本仓库推到 GitHub（或平台支持的 Git 源）。
2. 在平台选择 **Dockerfile 部署**，根目录即 `Dockerfile` 所在目录。
3. 在平台控制台配置与本地 `.env` 相同的密钥（DeepSeek / Supermind 等），**切勿**把 `.env` 提交进 Git。
4. 平台会为应用分配 **https://你的子域名.xxx.app** 一类地址，即阶段 A 的 HTTPS。

### Railway / Render / Fly.io

- 三者都会注入环境变量 **`PORT`**，镜像已用 `CMD` 读取 `${PORT:-8001}`，无需再改代码。
- 具体点击路径以各平台最新文档为准；核心是：**Build from Dockerfile + set env vars + public networking on**。

## 已知限制（阶段 A 可接受）

1. **Boss 抓取（DrissionPage + 浏览器）**：本镜像为精简版，**未安装 Chrome**。云上跑「职位抓取」可能失败；阶段 A 更适合作 **演示工作台 UI、已有数据、匹配/分析（LLM）**。完整浏览器抓取可留在本机，或后续单独做「带浏览器的镜像」阶段。
2. **数据持久化**：默认 `jobs.db` 在容器**可写层**；实例重启或换机后数据可能丢失。要持久化需平台 **Volume** 挂载到 `/app` 下 `jobs.db` 所在目录，或进入阶段 B 换云数据库。
3. **安全**：当前无登录，**不要**把含真实简历与密钥的实例长期公开；演示可加平台自带的 **IP 允许名单** 或简单 Basic Auth（后续可加）。

## 与后续阶段的关系

- **阶段 B（账号 + 多租户）**：再改 `db` 与 API，与阶段 A 的 Docker 部署方式兼容。
- 若只做面试展示：阶段 A + 一段录屏 + 架构说明通常已足够体现「懂 SaaS 交付路径」。
