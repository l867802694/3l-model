# 3L 股海雷达

一个面向 A 股趋势交易的本地数据生成器和静态看盘站点。

页面只回答三件事：

1. 当前市场环境是否适合趋势交易。
2. 东方财富行业板块中，哪些方向动量最强。
3. 哪些股票已经通过 250 日新高得到市场确认。

## 日常使用

```bash
# 更新最近完整交易日并同步静态站点
bash scripts/update-data.sh

# 启动本地页面
bash scripts/start-server.sh
```

本地页面：<http://localhost:8080/>
线上页面：<https://l867802694.github.io/3l-model/>

## 目录

- `assets/backend/update_data.py`：行情和模型计算入口
- `assets/backend/validate_data.py`：发布前数据校验
- `assets/backend/build_date_indexes.py`：本机与云端共用的日期索引生成器
- `.github/workflows/cloud-data-fallback.yml`：GitHub 云端数据兜底
- `assets/`：本地静态页面
- `scripts/`：更新、同步和自动发布脚本
- `deploy/`：独立的 GitHub Pages 仓库

数据源以 AkShare/BaoStock 行情为主，行业分类使用东方财富行业板块。本机在登录、17:00 和 20:00 检查数据；GitHub Actions 在交易日 18:37 进行独立兜底。发布前会同时检查收盘完整性、分类版本、行情覆盖率以及最近 5 日的股票池、板块范围和成交额异常。

## 回测验证

```bash
cd assets/backend
.venv/bin/python backtest_momentum_strength.py
```

报告同时保留单一起点的严格不重叠样本，以及覆盖全部持有起点的分组稳定性结果。不同起点之间可能共享持有期，因此稳健性结果用于识别起点敏感性，不直接触发模型参数调整。
