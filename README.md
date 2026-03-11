---
name: 3l-model
description: |
  A股3L模型分析系统 v1.0.6 - 修复动量页面日期选择器路径问题。
  
  版本: v1.0.6 (2026-03-10) - 稳定版本
  
  v1.0.6修复：
  - 修复动量页面日期选择器路径问题
  - 支持GitHub Pages子目录部署
  
  v1.0.5修复：
  - 一年新高筛选逻辑：移除0.999容差，只有收盘价>=250日最高价才算新高
  - 日期选择器：支持静态文件模式和API模式
  
  v1.0.4修复：
  - 一年新高涨跌幅计算：使用 change/pre_close 正确计算 pct_change
  - 突破幅度计算：修复突破幅度的计算逻辑
  - GitHub Pages兼容性：前端自动检测环境，静态托管使用 JSON 文件
  - 前端字段兼容性：支持 ts_code 和 code 字段

  v1.0.3修复：
  - 一年新高数据格式：修复为前端期望的 sectors 数组格式
  - 前端API端口：修复为 8000 端口
  - 依赖问题：添加 uvicorn、fastapi 到 requirements

  v1.0.2优化：
  - 排名变动颜色：上升=红色，下降=绿色
  - 排名逻辑：使用Tushare真实交易日历(pretrade_date)

  使用场景：
  - 分析A股板块动量排名和热点
  - 查看突破250日新高的强势股
  - 生成股票分析报告
  - 获取一年新高个股和板块分布

  触发词：3L模型、3l模型、板块动量、一年新高、股票分析、动量排名、
          新高个股、板块热点、A股分析、股票报告

  支持自动运行：每天下午5点自动更新数据并推送到GitHub Pages

  在线访问：https://l867802694.github.io/3l-model/
---

# 3L 模型 v1.0.6 - A股股票分析系统

## 版本信息

- **版本**: v1.0.6
- **发布日期**: 2026-03-10
- **状态**: 稳定版本 ✅

## 在线访问

- **首页**: https://l867802694.github.io/3l-model/
- **动量股池**: https://l867802694.github.io/3l-model/momentum.html
- **一年新高**: https://l867802694.github.io/3l-model/newhigh.html

## 概述

3L模型是一个A股股票分析工具，包含两个核心模型：

1. **动量模型**：基于20日涨幅筛选强势股，按东财二级行业分类统计板块动量
2. **一年新高模型**：统计突破250日最高价的个股，分析板块分布

## 更新日志

### v1.0.3 (2026-03-09) - 封板版本

- 🔧 修复一年新高数据格式（改为前端期望的 sectors 数组格式）
- 🔧 修复前端 API 端口配置（8001 → 8000）
- 🔧 添加缺失依赖（uvicorn、fastapi）
- ✅ 封板：此版本功能已完善，如需修改请开 v1.0.4+

### v1.0.2 (2026-03-08)

- 🎨 优化排名变动颜色（上升=红色，下降=绿色）
- 📅 优化排名逻辑（使用Tushare真实交易日历）

### v1.0.1 (2026-03-08)

- ✉️ 添加邮件通知功能

### v1.0.0 (2026-03-08)

- 🎉 初始版本发布

## 快速开始

### 查看网站

直接访问：
```
https://l867802694.github.io/3l-model/
```

### 本地运行

```bash
# 启动后端API
cd ~/.openclaw/skills/3l-model/assets/backend
source .venv/bin/activate
uvicorn api_server:app --host 0.0.0.0 --port 8000

# 启动前端（新开终端）
cd ~/.openclaw/skills/3l-model/assets
python -m http.server 8080
```

访问：
- 动量股池：http://localhost:8080/momentum_real.html
- 一年新高：http://localhost:8080/newhigh_final.html

### 手动更新数据

```bash
# 更新今天数据
~/.openclaw/skills/3l-model/scripts/auto-update-and-push.sh

# 或分步执行
cd ~/.openclaw/skills/3l-model/assets/backend
source .venv/bin/activate
python update_data.py
```

### 设置定时任务

```bash
~/.openclaw/skills/3l-model/scripts/setup-cron.sh
```

## 模型逻辑

### 动量模型

1. 获取全市场股票列表
2. 剔除次新股（上市<20天）和ST股票
3. 计算20日涨幅，取TOP 700
4. 机构资金过滤（市值+成交额代理指标）
5. 按东财二级行业分类统计
6. 计算动量分值 = 上榜数量 × 上榜占比

### 一年新高模型

1. 获取全市场股票
2. 过滤ST股和次新股
3. 计算250日最高价
4. 筛选收盘价突破250日最高的个股
5. 机构关注度过滤
6. 按行业统计板块分布

## 数据字段说明

### 动量模型

| 字段 | 说明 |
|------|------|
| momentum_score | 动量分值 |
| listed_count | 板块上榜股票数 |
| listed_ratio | 上榜占比 |
| avg_return_20d | 平均20日涨幅 |
| rank_change | 排名变动（较前一天）|

### 一年新高模型

| 字段 | 说明 |
|------|------|
| high_250 | 250日最高价 |
| close | 当日收盘价 |
| change_pct | 当日涨跌幅 |
| break_through | 突破幅度 |
| consecutive_days | 连新高天数 |

## 自动更新流程

```
每天 17:00 (下午5点)
    ↓
1. 从Tushare获取最新A股数据
    ↓
2. 计算板块动量排名
    ↓
3. 计算一年新高个股
    ↓
4. 推送到GitHub
    ↓
5. GitHub Pages自动部署
    ↓
朋友们看到最新数据！
```

## 文件结构

```
3l-model/
├── SKILL.md              # 本文件
├── skill.json            # 版本信息
├── 3l-model.sh           # 主入口脚本
├── scripts/              # 实用脚本
│   ├── setup-cron.sh
│   ├── start-server.sh
│   ├── update-data.sh
│   ├── auto-update-and-push.sh
│   └── deploy-to-github.sh
└── assets/               # 项目代码
    ├── backend/          # 后端代码
    │   ├── api_server.py
    │   ├── update_data.py
    │   └── data/         # 数据文件
    ├── momentum_real.html
    ├── newhigh_final.html
    └── index.html
```

## 依赖

- Python 3.10+
- FastAPI
- Tushare Pro API
- Git
- crontab (macOS/Linux)

## 配置

### Tushare Token

在 `assets/backend/update_data.py` 中配置：

```python
TUSHARE_TOKEN = "your-token-here"
TUSHARE_API_URL = "http://tushare.nlink.vip"
```

## 注意事项

1. **数据保留策略**：永久保存所有历史数据
2. **自动跳过周末和法定节假日**
3. **机构关注度使用市值+成交额作为代理指标**
4. **所有数据均为真实行情数据，非模拟生成**
5. **GitHub仓库只包含静态文件，核心代码和Token只在本地**

## 更新日志

### v1.0.6 (2026-03-10)

- 🔧 修复动量页面日期选择器路径问题
- 🔧 支持 GitHub Pages 子目录部署
- 🔧 统一前后端路径检测逻辑

### v1.0.5 (2026-03-10)

- 🔧 修复一年新高筛选逻辑：移除 `0.999` 容差，只有收盘价 >= 250日最高价才算新高
- 🔧 修复日期选择器：支持静态文件模式（GitHub Pages）和 API 模式（本地开发）
- 🔧 修复动量页面：支持 GitHub Pages 静态文件部署
- ✅ 数据已重新生成，现在只包含真正的新高股票

### v1.0.4 (2026-03-09) - 稳定版本

- 🔧 修复一年新高涨跌幅计算（使用 change/pre_close 正确计算）
- 🔧 修复突破幅度计算逻辑
- 🔧 修复 GitHub Pages 兼容性（静态托管自动使用 JSON 文件）
- 🔧 修复前端字段兼容性（支持 ts_code 和 code）
- ✅ 系统已稳定，明天自动更新将正常工作

### v1.0.3 (2026-03-09)

- 🔧 修复一年新高数据格式（改为前端期望的 sectors 数组格式）
- 🔧 修复前端 API 端口配置（8001 → 8000）
- 🔧 添加缺失依赖（uvicorn、fastapi）

### v1.0.2 (2026-03-08)

- 🎨 优化排名变动颜色（上升=红色，下降=绿色）
- 📅 优化排名逻辑（使用Tushare真实交易日历）

### v1.0.1 (2026-03-08)

- ✉️ 添加邮件通知功能

### v1.0.0 (2026-03-08)

- 🎉 初始版本发布
- ✅ 板块动量模型
- ✅ 一年新高模型
- ✅ 自动更新和部署
- ✅ GitHub Pages集成

## 许可证

MIT License

## 作者

ClawX

---

**3L 模型 v1.0.6 - 让股票分析更简单！** 📈
# Test SSH sync at Wed Mar 11 17:15:30 CST 2026
