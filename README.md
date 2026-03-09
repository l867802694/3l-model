# A股股票分析工具

基于动量模型和一年新高的A股股票分析工具，支持板块轮动分析和强势股追踪。

## 🌟 功能特性

### 📊 动量模型
- 计算20日涨幅前700只股票
- 剔除次新股（上市<20日）
- 机构资金过滤（基金≥2% 或 北向≥0.5%）
- 按东财二级行业分类统计
- 动量分值 = 板块上榜数量 × 上榜占比

### 📈 一年新高
- 统计突破250日最高价的个股
- 市场强度评分
- 板块分布统计
- 连新高天数追踪

## 🛠️ 技术栈

- **前端**: HTML + Tailwind CSS + JavaScript
- **后端**: Python + FastAPI
- **数据源**: Tushare Pro
- **数据库**: PostgreSQL (可选)
- **容器化**: Docker + Docker Compose

## 🚀 快速开始

### 方式一：Docker 一键部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/yourusername/stock-analyzer.git
cd stock-analyzer

# 2. 配置环境变量
cp backend/.env.example backend/.env
# 编辑 .env 文件，填入你的 Tushare Token

# 3. 启动服务
docker-compose up -d

# 4. 访问
# 前端: http://localhost
# API文档: http://localhost/api/docs
```

### 方式二：本地开发

```bash
# 1. 克隆项目
git clone https://github.com/yourusername/stock-analyzer.git
cd stock-analyzer

# 2. 安装后端依赖
cd backend
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 文件

# 4. 获取数据
python update_data.py

# 5. 启动后端
python api_server.py

# 6. 前端直接用浏览器打开 momentum_real.html
```

## ⚙️ 配置说明

### Tushare Token 配置

1. 注册 Tushare Pro: https://tushare.pro
2. 获取 Token
3. 在 `backend/.env` 中配置：

```env
TUSHARE_TOKEN=your_token_here
TUSHARE_API_URL=http://api.tushare.pro
```

### 定时任务（可选）

每天自动更新数据：

```bash
# macOS
launchctl load ~/Library/LaunchAgents/com.stockanalyzer.updatedata.plist

# Linux (cron)
0 17 * * * cd /path/to/stock-analyzer/backend && python update_data.py
```

## 📁 项目结构

```
stock-analyzer/
├── backend/              # 后端代码
│   ├── api_server.py     # FastAPI 主应用
│   ├── update_data.py    # 数据更新脚本
│   ├── requirements.txt  # Python 依赖
│   └── data/             # 数据文件（自动创建）
├── docker/               # Docker 配置
├── docs/                 # 文档
├── momentum_real.html    # 动量股池页面
├── newhigh_final.html    # 一年新高页面
├── docker-compose.yml    # Docker Compose 配置
└── README.md             # 本文件
```

## 🌐 API 接口

### 获取可用日期
```
GET /api/dates
```

### 动量模型
```
GET /api/momentum/latest          # 最新数据
GET /api/momentum/{date}          # 指定日期
GET /api/momentum/dates           # 可用日期列表
GET /api/momentum/sectors/{name}  # 板块详情
```

### 一年新高
```
GET /api/newhigh/latest           # 最新数据
GET /api/newhigh/{date}           # 指定日期
GET /api/newhigh/dates            # 可用日期列表
GET /api/newhigh/sectors/{name}   # 板块详情
```

## 📝 数据说明

- **数据来源**: Tushare Pro（5000积分档）
- **更新频率**: 每天下午5点（收盘后）
- **数据保留**: 永久保存历史数据
- **过滤规则**: 
  - 剔除ST/*ST/退市股票
  - 剔除上市不足20日的次新股
  - 只保留交易日数据（自动过滤周末和节假日）

## 🤝 贡献指南

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/xxx`
3. 提交更改：`git commit -am 'Add xxx'`
4. 推送分支：`git push origin feature/xxx`
5. 提交 Pull Request

## 📄 许可证

MIT License

## 🙏 致谢

- [Tushare](https://tushare.pro) - 提供股票数据
- [FastAPI](https://fastapi.tiangolo.com) - 后端框架
- [Tailwind CSS](https://tailwindcss.com) - 前端样式

---

⚠️ **免责声明**: 本项目仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。