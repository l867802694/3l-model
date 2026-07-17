#!/usr/bin/env python3
"""
数据更新脚本 - 使用 BaoStock 免费数据源

说明：
1. 不再依赖付费 Token
2. 历史日线数据使用 BaoStock，首次全量拉取会较慢
3. 原始历史数据按股票缓存到 raw_cache 目录，后续更新会复用缓存
"""

from __future__ import annotations

import argparse
import atexit
import bisect
import hashlib
import json
import math
import os
import re
import signal
import threading
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta
from functools import lru_cache
from math import ceil, sqrt
from pathlib import Path
from statistics import median
from tempfile import NamedTemporaryFile

import baostock as bs
import requests
import akshare as ak

# mx-xuangu 集成：用于补全东财行业/基金持股/250日新高信号
import sys as _sys
from pathlib import Path as _Path
_MX_SKILL_PATH = _Path.home() / ".openclaw" / "skills" / "mx-xuangu" / "mx_xuangu.py"
if _MX_SKILL_PATH.exists():
    _sys.path.insert(0, str(_MX_SKILL_PATH.parent))
    try:
        from mx_xuangu import MXSelectStock
        _MX_AVAILABLE = True
    except Exception:
        _MX_AVAILABLE = False
else:
    _MX_AVAILABLE = False

BACKEND_DIR = Path(__file__).parent
MODEL_CONFIG_PATH = BACKEND_DIR / "model_config.json"
MODEL_CONFIG = json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
MOMENTUM_CONFIG = MODEL_CONFIG["momentum"]
CLASSIFICATION_CONFIG = MODEL_CONFIG["classification"]

DATA_DIR = BACKEND_DIR / "data"
RAW_CACHE_DIR = BACKEND_DIR / "raw_cache" / "history"
RAW_CACHE_ROOT = BACKEND_DIR / "raw_cache"
INDUSTRY_CACHE_FILE = RAW_CACHE_ROOT / "eastmoney_industry_cache.json"
EASTMONEY_BOARD_CACHE_FILE = RAW_CACHE_ROOT / "eastmoney_board_industry_cache.json"
SHENWAN_CACHE_FILE = RAW_CACHE_ROOT / "shenwan_second_level_cache.json"
CLASSIFICATION_SNAPSHOT_FILE = BACKEND_DIR / CLASSIFICATION_CONFIG["snapshot_file"]
UPDATE_RUN_STATUS_FILE = RAW_CACHE_ROOT / "update_run_status.json"
SITE_STATUS_FILE = DATA_DIR / "site_status.json"
ENV_FILE = BACKEND_DIR / ".env"
def load_env_file() -> None:
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

MARKET_DATA_SOURCE = os.environ.get("MARKET_DATA_SOURCE", "akshare").strip().lower()
ACTIVE_DATA_SOURCE = MARKET_DATA_SOURCE
DEFAULT_WORKERS = max(1, int(os.environ.get("BAOSTOCK_MAX_WORKERS", "6")))
HISTORY_PROGRESS_TIMEOUT = max(
    30, int(os.environ.get("BAOSTOCK_PROGRESS_TIMEOUT", "120"))
)
CLASSIFICATION_WORKERS = max(
    1, int(os.environ.get("EASTMONEY_CLASSIFICATION_WORKERS", "12"))
)
SHENWAN_COMPONENT_WORKERS = max(
    1, int(os.environ.get("SHENWAN_COMPONENT_WORKERS", "8"))
)
MOMENTUM_TOP_RATIO = float(
    os.environ.get("MOMENTUM_TOP_RATIO", str(MOMENTUM_CONFIG["top_ratio"]))
)
MOMENTUM_SCORE_SCALE = float(os.environ.get("MOMENTUM_SCORE_SCALE", "1"))
MOMENTUM_MAINLINE_SCORE_MIN = float(
    os.environ.get(
        "MOMENTUM_MAINLINE_SCORE_MIN",
        str(MOMENTUM_CONFIG["mainline_score_min"]),
    )
)
MOMENTUM_CLIMAX_WARNING_SCORE_MIN = float(
    os.environ.get(
        "MOMENTUM_CLIMAX_WARNING_SCORE_MIN",
        os.environ.get(
            "MOMENTUM_WARNING_SCORE_MAX",
            str(MOMENTUM_CONFIG["climax_warning_score_min"]),
        ),
    )
)
MOMENTUM_MODEL_VERSION = str(MODEL_CONFIG["model_version"])
CLASSIFICATION_NAME = str(CLASSIFICATION_CONFIG["name"])
CLASSIFICATION_VERSION = str(CLASSIFICATION_CONFIG["version"])
CLASSIFICATION_AS_OF = str(CLASSIFICATION_CONFIG["as_of"])
MOMENTUM_SCORE_FORMULA = str(MOMENTUM_CONFIG["score_formula"])
INSTITUTION_MIN_SCORE = float(os.environ.get("INSTITUTION_MIN_SCORE", "2.5"))
HISTORY_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,isST"
)
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
}
EASTMONEY_CLIST_URLS = (
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://17.push2.eastmoney.com/api/qt/clist/get",
)
INDEX_BENCHMARKS = (
    ("sh.000001", "上证指数"),
    ("sh.000300", "沪深300"),
    ("sz.399006", "创业板指"),
    ("sh.000688", "科创50"),
)
MARKET_DISPLAY_INDEXES = (
    ("sh.000001", "上证指数", "shanghai", "沪市成交"),
    ("sz.399006", "创业板指", "chinext", "创业板成交"),
    ("sh.000688", "科创50", "star", "科创板成交"),
)
MIN_INDEX_DATE_MATCHES = max(
    1,
    min(
        len(INDEX_BENCHMARKS),
        int(os.environ.get("MIN_INDEX_DATE_MATCHES", "2")),
    ),
)
MIN_STOCK_DATE_COVERAGE = max(
    0.0,
    min(1.0, float(os.environ.get("MIN_STOCK_DATE_COVERAGE", "0.98"))),
)
MARKET_CLOSE_COMPLETE_HOUR = max(
    0, min(23, int(os.environ.get("MARKET_CLOSE_COMPLETE_HOUR", "16")))
)
MARKET_CLOSE_COMPLETE_MINUTE = max(
    0, min(59, int(os.environ.get("MARKET_CLOSE_COMPLETE_MINUTE", "10")))
)
DATA_QUALITY_VERSION = 1
BAOSTOCK_LOGIN_MAX_RETRIES = max(
    1, int(os.environ.get("BAOSTOCK_LOGIN_MAX_RETRIES", "5"))
)
BAOSTOCK_LOGIN_RETRY_SECONDS = max(
    1, int(os.environ.get("BAOSTOCK_LOGIN_RETRY_SECONDS", "15"))
)
BAOSTOCK_QUERY_MAX_RETRIES = max(
    1, int(os.environ.get("BAOSTOCK_QUERY_MAX_RETRIES", "3"))
)
BAOSTOCK_QUERY_RETRY_SECONDS = max(
    1, int(os.environ.get("BAOSTOCK_QUERY_RETRY_SECONDS", "5"))
)
AKSHARE_MAX_RETRIES = max(1, int(os.environ.get("AKSHARE_MAX_RETRIES", "3")))
AKSHARE_RETRY_SECONDS = max(1, int(os.environ.get("AKSHARE_RETRY_SECONDS", "5")))
AKSHARE_CALL_TIMEOUT_SECONDS = max(
    0, int(os.environ.get("AKSHARE_CALL_TIMEOUT_SECONDS", "60"))
)
AKSHARE_INCLUDE_BJ = os.environ.get("AKSHARE_INCLUDE_BJ", "0").strip().lower() in {
    "1",
    "true",
    "yes",
}
UPDATE_DEGRADED_MAX_SECONDS = max(
    60, int(os.environ.get("UPDATE_DEGRADED_MAX_SECONDS", "600"))
)
UPDATE_DEGRADED_MAX_FALLBACK_RATIO = max(
    0.0,
    min(
        1.0,
        float(os.environ.get("UPDATE_DEGRADED_MAX_FALLBACK_RATIO", "0.05")),
    ),
)
BULK_RUN_METRICS = {
    "attempted": 0,
    "written": 0,
    "fallback": 0,
    "duration_seconds": 0.0,
    "source": "none",
    "error": "",
}

DATA_DIR.mkdir(exist_ok=True)
RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
RAW_CACHE_ROOT.mkdir(parents=True, exist_ok=True)


def using_akshare() -> bool:
    return ACTIVE_DATA_SOURCE == "akshare"


def data_source_label() -> str:
    return "AkShare (免费数据)" if using_akshare() else "BaoStock (免费数据)"


def reset_bulk_run_metrics() -> None:
    BULK_RUN_METRICS.update(
        {
            "attempted": 0,
            "written": 0,
            "fallback": 0,
            "duration_seconds": 0.0,
            "source": "none",
            "error": "",
        }
    )


def normalize_date_str(date_str: str) -> str:
    return date_str.replace("-", "")


def normalize_industry_name(name) -> str:
    """Remove Shenwan level suffixes from industry names used in the UI."""
    text = str(name or "").strip()
    text = re.sub(r"\s*(?:Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ|VI|V|IV|III|II|I)\s*$", "", text)
    return text.strip() or "其他"


def calculate_classification_mapping_hash(mapping: dict) -> str:
    canonical = json.dumps(
        mapping,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@lru_cache(maxsize=1)
def load_frozen_classification_snapshot() -> dict | None:
    if not CLASSIFICATION_SNAPSHOT_FILE.exists():
        return None
    payload = json.loads(CLASSIFICATION_SNAPSHOT_FILE.read_text(encoding="utf-8"))
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise RuntimeError("东财分类快照缺少股票映射")
    if payload.get("classification_version") != CLASSIFICATION_VERSION:
        raise RuntimeError("东财分类快照版本与模型配置不一致")
    actual_hash = calculate_classification_mapping_hash(mapping)
    if payload.get("mapping_sha256") != actual_hash:
        raise RuntimeError("东财分类快照指纹校验失败")
    return payload


def build_classification_metadata(
    fallback_stats: dict | None = None,
    resolved_mapping: dict | None = None,
) -> dict:
    snapshot = load_frozen_classification_snapshot()
    fallback_stats = fallback_stats or {}
    fallback_count = int(
        fallback_stats.get("eastmoney_board", 0)
        + fallback_stats.get("eastmoney_quote", 0)
        + fallback_stats.get("unmapped", 0)
    )
    return {
        "classification_version": CLASSIFICATION_VERSION,
        "classification_as_of": CLASSIFICATION_AS_OF,
        "classification_mapping_hash": (
            snapshot.get("mapping_sha256") if snapshot else "unfrozen"
        ),
        "classification_fallback_count": fallback_count,
        "classification_fallback_breakdown": {
            "eastmoney_board": int(fallback_stats.get("eastmoney_board", 0)),
            "eastmoney_quote": int(fallback_stats.get("eastmoney_quote", 0)),
            "unmapped": int(fallback_stats.get("unmapped", 0)),
        },
        "classification_effective_mapping_hash": (
            calculate_classification_mapping_hash(resolved_mapping)
            if resolved_mapping
            else "unavailable"
        ),
    }


def hyphen_date(date_str: str) -> str:
    if "-" in date_str:
        return date_str
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def load_local_data(date_str: str, data_type: str, fallback_to_latest: bool = False):
    date_file = DATA_DIR / date_str / f"{data_type}.json"
    if date_file.exists():
        with open(date_file, "r", encoding="utf-8") as f:
            return json.load(f)

    if fallback_to_latest:
        latest_file = DATA_DIR / f"{data_type}_latest.json"
        if latest_file.exists():
            with open(latest_file, "r", encoding="utf-8") as f:
                return json.load(f)

    return None


def write_json_atomic(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def save_data(date_str: str, data_type: str, data):
    date_dir = DATA_DIR / date_str
    date_dir.mkdir(exist_ok=True)

    file_path = date_dir / f"{data_type}.json"
    write_json_atomic(file_path, data)
    write_json_atomic(DATA_DIR / f"{data_type}_latest.json", data)
    write_json_atomic(DATA_DIR / f"{data_type}.json", data)

    print(f"  ✓ 已保存: {file_path}")
    return file_path


def _parse_fund_pct(val) -> float:
    """解析基金持股字段，返回浮点数或0。"""
    try:
        s = str(val).split("|")[0].strip()
        return float(s)
    except Exception:
        return 0.0


def _ensure_mx_apikey() -> str | None:
    """确保 MX_APIKEY 在环境变量中，优先从 ~/.zshrc 读取。"""
    key = os.environ.get("MX_APIKEY")
    if key:
        return key
    # 从 ~/.zshrc 补充加载
    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        for line in zshrc.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export MX_APIKEY=") or line.startswith("MX_APIKEY="):
                if "=" in line:
                    return line.split("=", 1)[1].strip().strip("'\"").strip()
    return None


def _parse_turnover(val) -> float:
    """解析换手率/流通市值字段，返回浮点数或0。"""
    try:
        s = str(val).replace("%", "").replace("万", "e4").replace("亿", "e8").replace("元", "")
        return float(s)
    except Exception:
        return 0.0


def enrich_with_mx(
    stock_codes: list[str],
    mx_api_key: str | None = None,
) -> dict[str, dict]:
    """
    批量查询 mx-xuangu，给入选股票补全增强字段。

    返回 dict：{code: {
        "dfmc_industry": str,   # 东财行业总分类（三级）
        "dfmc_industry_l2": str, # 东财二级行业
        "fund_ratio": float,     # 基金持股占流通股本比例(%)
        "turnover_ratio": float, # 换手率(%)
        "is_250d_high": bool,    # 当日是否突破250日新高
    }}

    覆盖逻辑：
    - 用 "基金持股大于2% 东财行业" 条件一次查200条，覆盖入选股票
    - 用 "突破250日新高的股票" 条件一次查200条，命中即标记
    - 两张表拼起来，code 命中则填充对应字段
    """
    if not _MX_AVAILABLE:
        return {}
    if not stock_codes:
        return {}

    result: dict[str, dict] = {code: {} for code in stock_codes}

    # ① 查基金持股+东财行业
    try:
        mx = MXSelectStock(api_key=mx_api_key)
        rows_fund, src, err = mx.extract_data(mx.search("基金持股大于2% 东财行业"))
        if err or not rows_fund:
            print(f"    ⚠️ mx 基金持股+行业 查询失败: {err}")
        else:
            for row in rows_fund:
                code = str(row.get("代码", "")).strip()
                if code not in result:
                    continue
                result[code] = {
                    "dfmc_industry": str(row.get("东财行业总分类", "")),
                    "dfmc_industry_l2": str(row.get("东财行业分类二级", "")),
                    "fund_ratio": _parse_fund_pct(row.get("基金持股占流通股本比例(%) 截至2026.05.19最新", 0)),
                    "turnover_ratio": _parse_turnover(row.get("换手率(%) 2026.05.19", 0)),
                    "is_250d_high": False,
                }
    except Exception as exc:
        print(f"    ⚠️ mx 基金持股+行业 查询异常: {exc}")

    # ② 查250日新高
    try:
        mx = MXSelectStock(api_key=mx_api_key)
        rows_high, src, err = mx.extract_data(mx.search("突破250日新高的股票"))
        if err or not rows_high:
            print(f"    ⚠️ mx 250日新高 查询失败: {err}")
        else:
            peak_col = None
            # 找 "最高价区间最高 X" 格式的列名
            for col in rows_high[0].keys():
                if "最高价区间最高" in col:
                    peak_col = col
                    break

            if peak_col:
                for row in rows_high:
                    code = str(row.get("代码", "")).strip()
                    if code not in result:
                        continue
                    cur_price_col = "最新价(元) 2026.05.19"
                    cur_price_str = row.get(cur_price_col, "")
                    peak_str = row.get(peak_col, "")
                    try:
                        cur_price = float(cur_price_str)
                        peak = float(peak_str)
                        # 当日收盘价等于区间最高价 → 当日突破250日新高
                        if cur_price > 0 and abs(cur_price - peak) < 0.01:
                            result[code]["is_250d_high"] = True
                    except Exception:
                        pass
    except Exception as exc:
        print(f"    ⚠️ mx 250日新高 查询异常: {exc}")

    return result


def enrich_momentum_with_mx(trade_date: str, data: dict, mx_api_key: str | None = None) -> dict:
    """
    给 momentum JSON 的每只股票注入 mx 增强字段。

    追加字段（不影响原有字段）：
      dfmc_industry / dfmc_industry_l2 / fund_ratio / turnover_ratio / is_250d_high
    """
    stock_codes = []
    for sector in data.get("data", []):
        for stock in sector.get("stocks", []):
            code = str(stock.get("code", "")).strip()
            if code:
                stock_codes.append(code)

    if not stock_codes:
        return data

    print(f"  🏷️ MX字段回填: 查 {len(stock_codes)} 只股票...")
    mx_map = enrich_with_mx(stock_codes, mx_api_key)

    enriched = 0
    for sector in data.get("data", []):
        for stock in sector.get("stocks", []):
            code = str(stock.get("code", "")).strip()
            if code in mx_map:
                extra = mx_map[code]
                if extra:
                    stock.setdefault("dfmc_industry", extra.get("dfmc_industry", ""))
                    stock.setdefault("dfmc_industry_l2", extra.get("dfmc_industry_l2", ""))
                    stock.setdefault("fund_ratio", extra.get("fund_ratio", 0.0))
                    stock.setdefault("turnover_ratio", extra.get("turnover_ratio", 0.0))
                    stock.setdefault("is_250d_high", extra.get("is_250d_high", False))
                    enriched += 1

    print(f"  ✅ MX字段回填完成: {enriched} 只股票获增强字段")
    return data


def enrich_newhigh_with_mx(trade_date: str, data: dict, mx_api_key: str | None = None) -> dict:
    """
    给 newhigh JSON 的每只股票注入 mx 增强字段。
    """
    stock_codes = [str(s.get("code", "")).strip() for s in data.get("sectors", []) for s in s.get("stocks", [])]
    stock_codes = [c for c in stock_codes if c]

    if not stock_codes:
        return data

    print(f"  🏷️ MX字段回填: 查 {len(stock_codes)} 只股票...")
    mx_map = enrich_with_mx(stock_codes, mx_api_key)

    enriched = 0
    for sector in data.get("sectors", []):
        for stock in sector.get("stocks", []):
            code = str(stock.get("code", "")).strip()
            if code in mx_map:
                extra = mx_map[code]
                if extra:
                    stock.setdefault("dfmc_industry", extra.get("dfmc_industry", ""))
                    stock.setdefault("dfmc_industry_l2", extra.get("dfmc_industry_l2", ""))
                    stock.setdefault("fund_ratio", extra.get("fund_ratio", 0.0))
                    stock.setdefault("turnover_ratio", extra.get("turnover_ratio", 0.0))
                    stock.setdefault("is_250d_high", extra.get("is_250d_high", False))
                    enriched += 1

    print(f"  ✅ MX字段回填完成: {enriched} 只股票获增强字段")
    return data


def save_stock_industry_mapping():
    """生成股票代码→行业分类的速查表，从最新动量和新高数据中提取"""
    momentum_file = DATA_DIR / "momentum_latest.json"
    newhigh_file = DATA_DIR / "newhigh_latest.json"

    mapping = {}

    if momentum_file.exists():
        with open(momentum_file, encoding="utf-8") as f:
            d = json.load(f)
        for sector in d.get("data", []):
            sector_name = sector.get("sector_name", "")
            for stock in sector.get("stocks", []):
                code = stock.get("code", "")
                name = stock.get("name", "")
                if code and code not in mapping:
                    mapping[code] = {"name": name, "industry": sector_name}

    if newhigh_file.exists():
        with open(newhigh_file, encoding="utf-8") as f:
            d = json.load(f)
        for sector in d.get("sectors", []):
            sector_name = sector.get("sector_name", "")
            for stock in sector.get("stocks", []):
                code = stock.get("code", "")
                name = stock.get("name", "")
                if code and code not in mapping:
                    mapping[code] = {"name": name, "industry": sector_name}

    out_path = DATA_DIR / "stock_industry.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    print(f"  ✓ 股票行业速查表: {len(mapping)} 只股票 → {out_path.name}")
    return out_path


def filter_st_stocks(stocks):
    filtered = []
    for stock in stocks:
        name = str(stock.get("name", ""))
        if re.search(r"^(S\*?ST|\*?ST|退市|ST)", name):
            continue
        filtered.append(stock)
    return filtered


def resultset_to_dicts(rs):
    rows = []
    if rs.error_code != "0":
        raise RuntimeError(rs.error_msg)
    while rs.next():
        rows.append(dict(zip(rs.fields, rs.get_row_data())))
    return rows


def is_baostock_retryable_error(error) -> bool:
    message = str(error)
    retryable_signals = (
        "网络接收错误",
        "服务器连接失败",
        "接收数据异常",
        "连接失败",
        "Broken pipe",
        "timed out",
        "timeout",
        "Connection reset",
        "Connection aborted",
    )
    return any(signal in message for signal in retryable_signals)


@contextmanager
def limit_akshare_call_time(label: str):
    if (
        AKSHARE_CALL_TIMEOUT_SECONDS <= 0
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def raise_timeout(signum, frame):
        raise TimeoutError(
            f"AkShare {label} 超过 {AKSHARE_CALL_TIMEOUT_SECONDS} 秒未返回"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_timeout)
    previous_timer = signal.setitimer(
        signal.ITIMER_REAL, AKSHARE_CALL_TIMEOUT_SECONDS
    )
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL, previous_timer[0], previous_timer[1]
            )


def call_akshare(func, *args, context: str = "", **kwargs):
    label = context or getattr(func, "__name__", "AkShare")
    last_exc = None
    for attempt in range(1, AKSHARE_MAX_RETRIES + 1):
        try:
            with limit_akshare_call_time(label):
                return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt >= AKSHARE_MAX_RETRIES:
                raise
            print(
                f"  ⚠️ AkShare {label} 失败，第 {attempt}/{AKSHARE_MAX_RETRIES} 次重试前等待 "
                f"{AKSHARE_RETRY_SECONDS} 秒: {exc}"
            )
            time.sleep(AKSHARE_RETRY_SECONDS)
    raise last_exc


def login_baostock(max_retries: int = BAOSTOCK_LOGIN_MAX_RETRIES, context: str = ""):
    for attempt in range(1, max_retries + 1):
        lg = bs.login()
        if lg.error_code == "0":
            return

        error_msg = f"BaoStock 登录失败: {lg.error_msg}"
        if attempt >= max_retries or not is_baostock_retryable_error(error_msg):
            raise RuntimeError(error_msg)

        prefix = f"{context} " if context else ""
        print(
            f"⚠️ {prefix}BaoStock 登录失败，第 {attempt}/{max_retries} 次重试前等待 "
            f"{BAOSTOCK_LOGIN_RETRY_SECONDS} 秒: {lg.error_msg}"
        )
        time.sleep(BAOSTOCK_LOGIN_RETRY_SECONDS)


def logout_baostock():
    try:
        bs.logout()
    except Exception:
        pass


def execute_baostock_query(query_func, *args, **kwargs):
    for attempt in range(1, BAOSTOCK_QUERY_MAX_RETRIES + 1):
        rs = query_func(*args, **kwargs)
        error_code = getattr(rs, "error_code", None)
        error_msg = str(getattr(rs, "error_msg", ""))

        if error_code == "0":
            return rs

        if "用户未登录" in error_msg:
            if attempt >= BAOSTOCK_QUERY_MAX_RETRIES:
                return rs
            print(f"  ⚠️ BaoStock 会话失效，准备重新登录后重试查询 ({attempt}/{BAOSTOCK_QUERY_MAX_RETRIES})")
            login_baostock()
            time.sleep(1)
            continue

        if attempt >= BAOSTOCK_QUERY_MAX_RETRIES or not is_baostock_retryable_error(error_msg):
            return rs

        print(
            f"  ⚠️ BaoStock 查询失败，第 {attempt}/{BAOSTOCK_QUERY_MAX_RETRIES} 次重试前等待 "
            f"{BAOSTOCK_QUERY_RETRY_SECONDS} 秒: {error_msg}"
        )
        time.sleep(BAOSTOCK_QUERY_RETRY_SECONDS)

    return rs


def query_trade_dates(start_date: str, end_date: str):
    if using_akshare():
        df = call_akshare(ak.tool_trade_date_hist_sina, context="交易日历")
        trade_dates = []
        for value in df.get("trade_date", []):
            date_str = normalize_date_str(str(value).split()[0])
            if start_date <= date_str <= end_date:
                trade_dates.append(date_str)
        return sorted(set(trade_dates))

    for attempt in range(2):
        rs = execute_baostock_query(
            bs.query_trade_dates,
            start_date=hyphen_date(start_date),
            end_date=hyphen_date(end_date),
        )
        try:
            rows = resultset_to_dicts(rs)
            trade_dates = []
            for row in rows:
                date_value = (
                    row.get("calendar_date") or row.get("date") or next(iter(row.values()))
                )
                is_trading = row.get("is_trading_day")
                if is_trading is None:
                    values = list(row.values())
                    is_trading = values[1] if len(values) > 1 else "0"
                if str(is_trading) == "1":
                    trade_dates.append(normalize_date_str(date_value))
            return trade_dates
        except RuntimeError as exc:
            if "用户未登录" not in str(exc) or attempt == 1:
                raise
            login_baostock()

    return []


def is_market_close_complete(trade_date: str, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    if trade_date < today:
        return True
    if trade_date > today:
        return False
    return (now.hour, now.minute) >= (
        MARKET_CLOSE_COMPLETE_HOUR,
        MARKET_CLOSE_COMPLETE_MINUTE,
    )


def get_trade_date(
    n_days_ago: int = 0,
    allow_intraday: bool = False,
    now: datetime | None = None,
):
    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    start_date = (now - timedelta(days=90)).strftime("%Y%m%d")
    trade_dates = query_trade_dates(start_date, today)
    if not trade_dates:
        raise RuntimeError("未获取到交易日历")
    if (
        not allow_intraday
        and trade_dates[-1] == today
        and not is_market_close_complete(today, now=now)
    ):
        trade_dates = trade_dates[:-1]
    if not trade_dates:
        raise RuntimeError("没有可用的完整收盘交易日")
    if n_days_ago >= len(trade_dates):
        raise RuntimeError("交易日范围不足")
    return trade_dates[-1 - n_days_ago]


def get_trade_dates_in_range(start_date: str, end_date: str):
    return query_trade_dates(start_date, end_date)


def get_prev_trade_date(date_str: str):
    start_date = (
        datetime.strptime(date_str, "%Y%m%d") - timedelta(days=30)
    ).strftime("%Y%m%d")
    trade_dates = query_trade_dates(start_date, date_str)
    if len(trade_dates) < 2:
        return None
    return trade_dates[-2]


def get_recent_trade_dates(end_date: str, window: int = 5):
    start_date = (
        datetime.strptime(end_date, "%Y%m%d") - timedelta(days=60)
    ).strftime("%Y%m%d")
    trade_dates = query_trade_dates(start_date, end_date)
    if not trade_dates:
        return []
    return trade_dates[-window:]


def moving_average(rows, window: int, offset: int = 0):
    end = len(rows) - offset
    start = end - window
    if start < 0 or end <= 0 or end - start < window:
        return 0.0
    closes = [safe_float(row.get("close")) for row in rows[start:end]]
    valid = [value for value in closes if value > 0]
    if len(valid) < window:
        return 0.0
    return sum(valid) / window


def eastmoney_quote_url(code: str):
    market, stock_code = code.split(".", 1)
    if market in {"sh", "sz"}:
        return f"https://quote.eastmoney.com/{market}{stock_code}.html"
    if market == "bj":
        return f"https://quote.eastmoney.com/unify/r/0.{stock_code}"
    return None


def load_industry_cache():
    if not INDUSTRY_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(INDUSTRY_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_industry_cache(cache):
    INDUSTRY_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_eastmoney_board_cache():
    if not EASTMONEY_BOARD_CACHE_FILE.exists():
        return None
    try:
        return json.loads(EASTMONEY_BOARD_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_eastmoney_board_cache(payload):
    EASTMONEY_BOARD_CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_cache_from_today(cache) -> bool:
    return (cache or {}).get("generated_on") == datetime.now().strftime("%Y%m%d")


def eastmoney_request_json(params: dict, retries: int = 3) -> dict:
    last_error = None
    session = requests.Session()
    session.trust_env = False
    for attempt in range(1, retries + 1):
        for url in EASTMONEY_CLIST_URLS:
            try:
                response = session.get(
                    url,
                    params=params,
                    headers=DEFAULT_HEADERS,
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("rc") != 0:
                    raise RuntimeError(f"Eastmoney rc={payload.get('rc')}")
                return payload
            except Exception as exc:
                last_error = exc
        if attempt < retries:
            time.sleep(0.8 * attempt)
    raise RuntimeError(f"Eastmoney board request failed: {last_error}")


def fetch_eastmoney_industry_boards() -> list[dict]:
    boards = []
    page = 1
    page_size = 100
    total = None

    while total is None or len(boards) < total:
        payload = eastmoney_request_json(
            {
                "pn": page,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:90 t:2 f:!50",
                "fields": "f12,f14,f3,f20,f21,f104,f105,f128,f140,f136,f152",
            }
        )
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        rows = data.get("diff", []) or []
        if not rows:
            break
        boards.extend(rows)
        page += 1

    deduped = {}
    for item in boards:
        code = item.get("f12")
        name = item.get("f14")
        if not code or not name:
            continue
        deduped[code] = {
            "board_code": code,
            "board_name": str(name).strip(),
            "change_pct": item.get("f3"),
            "up_count": item.get("f104"),
            "down_count": item.get("f105"),
            "leading_stock": item.get("f128"),
        }
    return list(deduped.values())


def selected_eastmoney_board_names_from_quote_cache(cache: dict) -> tuple[set[str], set[str]]:
    raw_names = set()
    normalized_names = set()
    for item in cache.values():
        name = str((item or {}).get("industry") or "").strip()
        if not name or name == "其他":
            continue
        raw_names.add(name)
        normalized_names.add(normalize_industry_name(name))
    return raw_names, normalized_names


def select_eastmoney_middle_boards(boards: list[dict], quote_cache: dict) -> list[dict]:
    raw_names, normalized_names = selected_eastmoney_board_names_from_quote_cache(
        quote_cache
    )
    if not normalized_names:
        return [
            board
            for board in boards
            if "Ⅲ" not in board["board_name"]
        ]

    grouped = {}
    for board in boards:
        normalized = normalize_industry_name(board["board_name"])
        if normalized not in normalized_names:
            continue
        grouped.setdefault(normalized, []).append(board)

    selected = []
    for normalized, candidates in grouped.items():
        exact = [item for item in candidates if item["board_name"] in raw_names]
        if exact:
            selected.extend(exact)
            continue

        second_level = [item for item in candidates if "Ⅱ" in item["board_name"]]
        if second_level:
            selected.extend(second_level)
            continue

        non_third_level = [item for item in candidates if "Ⅲ" not in item["board_name"]]
        selected.extend(non_third_level or candidates[:1])

    deduped = {}
    for board in selected:
        deduped[board["board_code"]] = board
    return list(deduped.values())


def fetch_eastmoney_board_components(board: dict) -> tuple[dict, list[dict]]:
    rows = []
    page = 1
    page_size = 100
    total = None
    while total is None or len(rows) < total:
        payload = eastmoney_request_json(
            {
                "pn": page,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": f"b:{board['board_code']}",
                "fields": "f12,f14,f2,f3,f5,f6,f20,f21,f62,f184",
            }
        )
        data = payload.get("data", {}) or {}
        total = int(data.get("total") or 0)
        page_rows = data.get("diff", []) or []
        if not page_rows:
            break
        rows.extend(page_rows)
        page += 1

    stocks = [
        {
            "code": str(item.get("f12", "")).zfill(6),
            "name": item.get("f14"),
        }
        for item in rows
        if item.get("f12") and item.get("f14")
    ]
    return board, stocks


def choose_eastmoney_board_for_stock(
    code: str,
    memberships: list[dict],
    quote_cache: dict,
) -> dict:
    quote_info = quote_cache.get(code) or {}
    quote_bk_id = quote_info.get("bk_id")
    quote_industry = normalize_industry_name(quote_info.get("industry"))

    if quote_bk_id:
        for item in memberships:
            if item["board_code"] == quote_bk_id:
                return item

    if quote_industry and quote_industry != "其他":
        for item in memberships:
            if normalize_industry_name(item["board_name"]) == quote_industry:
                return item

    return sorted(
        memberships,
        key=lambda item: (
            int(item.get("stock_count") or 0),
            "Ⅱ" in item["board_name"],
            "Ⅲ" not in item["board_name"],
        ),
        reverse=True,
    )[0]


def build_eastmoney_board_industry_cache(quote_cache: dict) -> dict:
    print("  🏷️ 拉取东财行业板块列表...")
    all_boards = fetch_eastmoney_industry_boards()
    selected_boards = select_eastmoney_middle_boards(all_boards, quote_cache)
    print(
        f"    东财行业板块: 全量 {len(all_boards)} 个，"
        f"选用中层 {len(selected_boards)} 个"
    )

    board_components = {}
    stock_memberships = {}
    max_workers = min(CLASSIFICATION_WORKERS, max(1, len(selected_boards)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_eastmoney_board_components, board): board["board_code"]
            for board in selected_boards
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            board, components = future.result()
            board_code = board["board_code"]
            board_info = {
                **board,
                "stock_count": len(components),
                "normalized_name": normalize_industry_name(board["board_name"]),
            }
            board_components[board_code] = board_info
            for stock in components:
                stock_memberships.setdefault(stock["code"], []).append(board_info)
            if idx % 25 == 0 or idx == len(selected_boards):
                print(f"    已完成东财板块成分 {idx}/{len(selected_boards)}")

    mapping = {}
    for code, memberships in stock_memberships.items():
        chosen = choose_eastmoney_board_for_stock(code, memberships, quote_cache)
        mapping[code] = {
            "industry": normalize_industry_name(chosen["board_name"]),
            "board_name": chosen["board_name"],
            "board_code": chosen["board_code"],
            "source": "eastmoney_board",
        }

    payload = {
        "generated_on": datetime.now().strftime("%Y%m%d"),
        "source": "eastmoney_push2_industry_boards",
        "board_query_fs": "m:90 t:2 f:!50",
        "component_query_fs": "b:{board_code}",
        "all_board_count": len(all_boards),
        "selected_board_count": len(selected_boards),
        "stock_count": len(mapping),
        "selected_boards": sorted(
            board_components.values(), key=lambda item: item["board_name"]
        ),
        "mapping": mapping,
    }
    save_eastmoney_board_cache(payload)
    return payload


def load_or_build_eastmoney_board_industry_cache(quote_cache: dict) -> dict:
    cache = load_eastmoney_board_cache()
    if cache and cache.get("mapping") and is_cache_from_today(cache):
        return cache
    if cache and cache.get("mapping") and not quote_cache:
        return cache
    return build_eastmoney_board_industry_cache(quote_cache)


@lru_cache(maxsize=1)
def load_legacy_sw_sector_fallback_map():
    code_to_sector = {}
    for pattern, sector_key, stocks_key in (
        ("20*/momentum.json", "sector_name", "data"),
        ("20*/newhigh.json", "sector_name", "sectors"),
    ):
        for file_path in DATA_DIR.glob(pattern):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("classification") != "申万2021二级行业":
                continue
            for sector in data.get(stocks_key, []):
                sector_name = sector.get(sector_key)
                for stock in sector.get("stocks", []):
                    code = stock.get("code")
                    if code and sector_name and sector_name != "其他":
                        code_to_sector[code] = sector_name
    return code_to_sector


def load_shenwan_cache():
    if not SHENWAN_CACHE_FILE.exists():
        return None
    try:
        return json.loads(SHENWAN_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_shenwan_cache(mapping):
    payload = {
        "generated_on": datetime.now().strftime("%Y%m%d"),
        "mapping": mapping,
    }
    SHENWAN_CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_shenwan_component_stocks(task):
    industry_code, industry_name = task
    url = "https://www.swsresearch.com/institute-sw/api/index_publish/details/component_stocks/"
    response = requests.get(
        url,
        params={"swindexcode": industry_code, "page": "1", "page_size": "10000"},
        headers=DEFAULT_HEADERS,
        timeout=30,
        verify=False,
    )
    response.raise_for_status()
    data = response.json()["data"]["results"]
    return industry_name, [item["stockcode"] for item in data]


def build_shenwan_current_mapping():
    second_level = call_akshare(
        ak.index_realtime_sw, symbol="二级行业", context="申万二级行业列表"
    )[["指数代码", "指数名称"]]
    tasks = [
        (row["指数代码"], row["指数名称"])
        for _, row in second_level.iterrows()
    ]
    mapping = {}
    max_workers = min(SHENWAN_COMPONENT_WORKERS, len(tasks))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_shenwan_component_stocks, task): task[0] for task in tasks
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            industry_name, stock_codes = future.result()
            for stock_code in stock_codes:
                mapping[stock_code] = industry_name
            if idx % 20 == 0 or idx == len(tasks):
                print(f"    已完成申万成分 {idx}/{len(tasks)}")
    save_shenwan_cache(mapping)
    return mapping


def resolve_shenwan_industry_map(stocks):
    cache = load_shenwan_cache()
    cache_mapping = cache.get("mapping", {}) if cache else {}
    if not cache_mapping:
        print("  🏷️ 构建申万二级分类映射...")
        cache_mapping = build_shenwan_current_mapping()

    fallback_mapping = load_legacy_sw_sector_fallback_map()
    industry_map = {}
    stats = {"shenwan_components": 0, "legacy_data": 0, "unmapped": 0}

    for stock in stocks:
        stock_code = stock["code"].split(".")[1]
        industry_name = cache_mapping.get(stock_code)
        if industry_name:
            industry_map[stock["code"]] = normalize_industry_name(industry_name)
            stats["shenwan_components"] += 1
            continue

        fallback_name = fallback_mapping.get(stock_code)
        if fallback_name:
            industry_map[stock["code"]] = normalize_industry_name(fallback_name)
            stats["legacy_data"] += 1
            continue

        stats["unmapped"] += 1

    return industry_map, stats


def parse_eastmoney_industry_from_html(text: str):
    quotedata_match = re.search(r"var\s+quotedata\s*=\s*(\{.*?\});", text)
    if quotedata_match:
        try:
            quotedata = json.loads(quotedata_match.group(1))
        except json.JSONDecodeError:
            quotedata = {}
        industry_name = quotedata.get("bk_name")
        if industry_name:
            return industry_name, quotedata.get("bk_id")

    related_match = re.search(r"\[<a [^>]+>([^<]+)</a>\]相关个股", text)
    if related_match:
        return related_match.group(1).strip(), None

    return None, None


def fetch_eastmoney_industry(task):
    code, name = task
    url = eastmoney_quote_url(code)
    if not url:
        return code, {"industry": None, "bk_id": None, "source": "unsupported"}

    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        response.raise_for_status()
        industry_name, bk_id = parse_eastmoney_industry_from_html(response.text)
        if industry_name:
            return code, {
                "industry": industry_name,
                "bk_id": bk_id,
                "source": "eastmoney_quote",
            }
    except Exception:
        pass

    return code, {"industry": None, "bk_id": None, "source": "unmapped"}


def resolve_live_eastmoney_industry_map(stocks):
    cache = load_industry_cache()
    industry_map = {}
    fallback_stats = {
        "eastmoney_board": 0,
        "eastmoney_quote": 0,
        "legacy_data": 0,
        "unmapped": 0,
    }

    try:
        board_cache = load_or_build_eastmoney_board_industry_cache(cache)
        board_mapping = board_cache.get("mapping", {}) if board_cache else {}
        for stock in stocks:
            stock_code = plain_code(stock["code"])
            info = board_mapping.get(stock_code)
            if info and info.get("industry"):
                industry_map[stock["code"]] = info["industry"]
                fallback_stats["eastmoney_board"] += 1
                continue

            cached = cache.get(stock["code"])
            if cached and cached.get("industry"):
                industry_map[stock["code"]] = normalize_industry_name(cached["industry"])
                fallback_stats[cached.get("source", "eastmoney_quote")] = (
                    fallback_stats.get(cached.get("source", "eastmoney_quote"), 0) + 1
                )
                continue

            fallback_stats["unmapped"] += 1

        if fallback_stats["eastmoney_board"]:
            return industry_map, fallback_stats
    except Exception as exc:
        print(f"  ⚠️ 东财行业板块映射失败，回退到个股页面分类: {exc}")

    industry_map = {}
    fallback_stats = {"eastmoney_quote": 0, "legacy_data": 0, "unmapped": 0}
    tasks = []

    for stock in stocks:
        cached = cache.get(stock["code"])
        if cached and cached.get("industry"):
            industry_map[stock["code"]] = normalize_industry_name(cached["industry"])
            fallback_stats[cached.get("source", "eastmoney_quote")] = (
                fallback_stats.get(cached.get("source", "eastmoney_quote"), 0) + 1
            )
            continue
        tasks.append((stock["code"], stock["name"]))

    if tasks:
        print(f"  🏷️ 需补全东财分类: {len(tasks)}只")
        max_workers = min(CLASSIFICATION_WORKERS, len(tasks))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fetch_eastmoney_industry, task): task[0] for task in tasks
            }
            for idx, future in enumerate(as_completed(futures), start=1):
                code, info = future.result()
                cache[code] = info
                if info.get("industry"):
                    industry_map[code] = normalize_industry_name(info["industry"])
                fallback_stats[info.get("source", "unmapped")] = (
                    fallback_stats.get(info.get("source", "unmapped"), 0) + 1
                )
                if idx % 200 == 0 or idx == len(tasks):
                    print(f"    已完成分类 {idx}/{len(tasks)}")
        save_industry_cache(cache)

    return industry_map, fallback_stats


def resolve_eastmoney_industry_map(stocks):
    snapshot = load_frozen_classification_snapshot()
    if not snapshot:
        return resolve_live_eastmoney_industry_map(stocks)

    frozen_mapping = snapshot["mapping"]
    industry_map = {}
    missing_stocks = []
    for stock in stocks:
        industry = frozen_mapping.get(plain_code(stock["code"]))
        if industry:
            industry_map[stock["code"]] = normalize_industry_name(industry)
        else:
            missing_stocks.append(stock)

    fallback_stats = {
        "frozen_snapshot": len(industry_map),
        "eastmoney_board": 0,
        "eastmoney_quote": 0,
        "legacy_data": 0,
        "unmapped": 0,
    }
    if missing_stocks:
        quote_cache = load_industry_cache()
        unresolved = []
        for stock in missing_stocks:
            cached = quote_cache.get(stock["code"])
            industry = normalize_industry_name((cached or {}).get("industry"))
            if cached and industry != "其他":
                industry_map[stock["code"]] = industry
                fallback_stats["eastmoney_quote"] += 1
            else:
                unresolved.append(stock)
        missing_stocks = unresolved

    if missing_stocks:
        live_map, live_stats = resolve_live_eastmoney_industry_map(missing_stocks)
        industry_map.update(live_map)
        for key, value in live_stats.items():
            fallback_stats[key] = fallback_stats.get(key, 0) + int(value or 0)

    return industry_map, fallback_stats


def market_code_from_plain(code: str) -> str | None:
    code = str(code).strip().zfill(6)
    if code.startswith(("60", "68", "69")):
        return f"sh.{code}"
    if code.startswith(("00", "30", "20")):
        return f"sz.{code}"
    if code.startswith(("43", "83", "87", "88", "89", "92")):
        return f"bj.{code}"
    return None


def plain_code(code: str) -> str:
    return str(code).split(".")[-1].zfill(6)


def normalize_akshare_listing_date(value) -> str | None:
    date_str = str(value or "").strip()
    if not date_str or date_str.lower() in {"nan", "none"}:
        return None
    return normalize_date_str(date_str.split()[0])


def load_akshare_stock_name_map():
    try:
        df = call_akshare(ak.stock_info_a_code_name, context="A股代码名称")
    except Exception as exc:
        print(f"  ⚠️ AkShare 股票名称列表获取失败，将使用各交易所简称: {exc}")
        return {}

    return {
        str(row.get("code", "")).zfill(6): str(row.get("name", "")).strip()
        for _, row in df.iterrows()
        if str(row.get("code", "")).strip()
    }


def append_akshare_stock_rows(stocks_by_code, rows, code_col, name_col, date_col, name_map):
    for _, row in rows.iterrows():
        raw_code = str(row.get(code_col, "")).strip().zfill(6)
        code = market_code_from_plain(raw_code)
        if not code:
            continue
        list_date = normalize_akshare_listing_date(row.get(date_col))
        if not list_date:
            continue
        stocks_by_code[code] = {
            "code": code,
            "name": name_map.get(raw_code) or str(row.get(name_col, "")).strip(),
            "list_date": list_date,
            "industry": "其他",
        }


def load_akshare_stock_universe(trade_date: str, stock_limit: int | None = None):
    print("  📋 加载股票基础信息 (AkShare)...")
    current_date = datetime.strptime(trade_date, "%Y%m%d")
    name_map = load_akshare_stock_name_map()
    stocks_by_code = {}

    exchange_sources = (
        ("沪市主板", lambda: ak.stock_info_sh_name_code(symbol="主板A股"), "证券代码", "证券简称", "上市日期"),
        ("沪市科创板", lambda: ak.stock_info_sh_name_code(symbol="科创板"), "证券代码", "证券简称", "上市日期"),
        ("深市A股", lambda: ak.stock_info_sz_name_code(symbol="A股列表"), "A股代码", "A股简称", "A股上市日期"),
    )
    if AKSHARE_INCLUDE_BJ:
        exchange_sources = exchange_sources + (
            ("北交所", ak.stock_info_bj_name_code, "证券代码", "证券简称", "上市日期"),
        )

    for label, loader, code_col, name_col, date_col in exchange_sources:
        try:
            rows = call_akshare(loader, context=f"{label}股票列表")
            append_akshare_stock_rows(stocks_by_code, rows, code_col, name_col, date_col, name_map)
        except Exception as exc:
            print(f"  ⚠️ {label} 股票列表获取失败，跳过该交易所: {exc}")

    stocks = []
    for stock in stocks_by_code.values():
        try:
            list_date = datetime.strptime(stock["list_date"], "%Y%m%d")
        except ValueError:
            continue
        list_days = (current_date - list_date).days
        if list_days < 20:
            continue
        stock["list_days"] = list_days
        stocks.append(stock)

    stocks.sort(key=lambda item: item["code"])
    stocks = filter_st_stocks(stocks)
    if stock_limit:
        stocks = stocks[:stock_limit]

    industry_map, fallback_stats = resolve_eastmoney_industry_map(stocks)

    for stock in stocks:
        stock["industry"] = industry_map.get(stock["code"], "其他")

    print(f"  ✓ 股票池: {len(stocks)}只")
    print(
        "  ✓ 东财分类来源: "
        f"V3冻结映射 {fallback_stats.get('frozen_snapshot', 0)} | "
        f"东财板块成分 {fallback_stats.get('eastmoney_board', 0)} | "
        f"东财页面/缓存 {fallback_stats.get('eastmoney_quote', 0)} | "
        f"未映射 {fallback_stats.get('unmapped', 0)}"
    )
    resolved_mapping = {
        plain_code(stock["code"]): stock["industry"] for stock in stocks
    }
    return stocks, CLASSIFICATION_NAME, build_classification_metadata(
        fallback_stats,
        resolved_mapping,
    )


def load_stock_universe(trade_date: str, stock_limit: int | None = None):
    if using_akshare():
        return load_akshare_stock_universe(trade_date, stock_limit)

    print("  📋 加载股票基础信息...")
    basic_rows = resultset_to_dicts(execute_baostock_query(bs.query_stock_basic))

    current_date = datetime.strptime(trade_date, "%Y%m%d")
    stocks = []
    for row in basic_rows:
        code = row.get("code", "")
        if row.get("type") != "1" or row.get("status") != "1":
            continue
        if not code.startswith(("sh.", "sz.", "bj.")):
            continue
        ipo_date = row.get("ipoDate") or ""
        if not ipo_date:
            continue
        try:
            list_date = datetime.strptime(ipo_date, "%Y-%m-%d")
        except ValueError:
            continue
        list_days = (current_date - list_date).days
        if list_days < 20:
            continue

        stocks.append(
            {
                "code": code,
                "name": row.get("code_name", ""),
                "list_date": normalize_date_str(ipo_date),
                "list_days": list_days,
                "industry": "其他",
            }
        )

    stocks = filter_st_stocks(stocks)
    if stock_limit:
        stocks = stocks[:stock_limit]

    industry_map, fallback_stats = resolve_eastmoney_industry_map(stocks)

    for stock in stocks:
        stock["industry"] = industry_map.get(stock["code"], "其他")

    print(f"  ✓ 股票池: {len(stocks)}只")
    print(
        "  ✓ 东财分类来源: "
        f"V3冻结映射 {fallback_stats.get('frozen_snapshot', 0)} | "
        f"东财板块成分 {fallback_stats.get('eastmoney_board', 0)} | "
        f"东财页面/缓存 {fallback_stats.get('eastmoney_quote', 0)} | "
        f"未映射 {fallback_stats.get('unmapped', 0)}"
    )
    resolved_mapping = {
        plain_code(stock["code"]): stock["industry"] for stock in stocks
    }
    return stocks, CLASSIFICATION_NAME, build_classification_metadata(
        fallback_stats,
        resolved_mapping,
    )


def history_cache_path(code: str) -> Path:
    return RAW_CACHE_DIR / f"{code.replace('.', '_')}.json"


def load_cached_history(code: str):
    cache_file = history_cache_path(code)
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_cached_history(code: str, rows):
    cache_file = history_cache_path(code)
    write_json_atomic(cache_file, rows)


def subset_history(rows, start_date: str, end_date: str):
    return [
        row
        for row in rows
        if start_date <= normalize_date_str(row["date"]) <= end_date
    ]


def merge_history_rows(existing_rows, new_rows):
    merged = {}
    for row in existing_rows or []:
        merged[normalize_date_str(row["date"])] = row
    for row in new_rows or []:
        key = normalize_date_str(row["date"])
        merged[key] = row
    return [merged[key] for key in sorted(merged)]


def akshare_row_value(row, *names, default=""):
    for name in names:
        if name in row:
            value = row.get(name)
            if value is not None:
                return value
    return default


def convert_akshare_stock_history_rows(df, code: str):
    rows = []
    previous_close = None
    for _, row in df.iterrows():
        close = safe_float(akshare_row_value(row, "收盘", "close"))
        explicit_pct_chg = akshare_row_value(row, "涨跌幅", "pctChg", default=None)
        pct_chg = safe_float(explicit_pct_chg) if explicit_pct_chg is not None else 0
        if previous_close is None:
            previous_close = (
                close / (1 + pct_chg / 100)
                if close > 0 and explicit_pct_chg is not None and pct_chg != -100
                else close
            )
        elif explicit_pct_chg is None:
            pct_chg = (
                (close - previous_close) / previous_close * 100
                if previous_close > 0
                else 0
            )

        date_value = akshare_row_value(row, "日期", "date")
        turnover = safe_float(akshare_row_value(row, "换手率", "turn", "turnover"))
        if "turnover" in row and "换手率" not in row and turnover <= 1:
            turnover *= 100
        rows.append(
            {
                "date": hyphen_date(normalize_date_str(str(date_value).split()[0])),
                "code": code,
                "open": str(safe_float(akshare_row_value(row, "开盘", "open"))),
                "high": str(safe_float(akshare_row_value(row, "最高", "high"))),
                "low": str(safe_float(akshare_row_value(row, "最低", "low"))),
                "close": str(close),
                "preclose": str(previous_close),
                "volume": str(safe_float(akshare_row_value(row, "成交量", "volume"))),
                "amount": str(safe_float(akshare_row_value(row, "成交额", "amount"))),
                "turn": str(turnover),
                "pctChg": str(pct_chg),
                "isST": "0",
            }
        )
        previous_close = close
    return rows


def convert_akshare_spot_records(records: list[dict], trade_date: str) -> dict[str, dict]:
    rows = {}
    for record in records:
        raw_code = str(record.get("代码", "")).strip().lower()
        if raw_code.startswith(("sh", "sz", "bj")):
            raw_code = raw_code[2:]
        code = market_code_from_plain(raw_code)
        close = safe_float(record.get("最新价"))
        preclose = safe_float(record.get("昨收"))
        if not code or close <= 0 or preclose <= 0:
            continue
        rows[code] = {
            "date": hyphen_date(trade_date),
            "code": code,
            "open": str(safe_float(record.get("今开"))),
            "high": str(safe_float(record.get("最高"))),
            "low": str(safe_float(record.get("最低"))),
            "close": str(close),
            "preclose": str(preclose),
            "volume": str(safe_float(record.get("成交量"))),
            "amount": str(safe_float(record.get("成交额"))),
            "turn": str(safe_float(record.get("换手率"))),
            "pctChg": str(safe_float(record.get("涨跌幅"))),
            "isST": "1" if "ST" in str(record.get("名称") or "").upper() else "0",
        }
    return rows


def fetch_bulk_latest_stock_rows_akshare(trade_date: str) -> dict[str, dict]:
    try:
        frame = call_akshare(
            ak.stock_zh_a_spot,
            context=f"{trade_date}新浪全市场快照",
        )
        BULK_RUN_METRICS["source"] = "sina"
    except Exception as sina_exc:
        print(f"  ⚠️ 新浪全市场快照失败，尝试东财快照: {sina_exc}")
        frame = call_akshare(
            ak.stock_zh_a_spot_em,
            context=f"{trade_date}东财全市场快照",
        )
        BULK_RUN_METRICS["source"] = "eastmoney"
    return convert_akshare_spot_records(frame.to_dict("records"), trade_date)


def select_bulk_increment_tasks(tasks, previous_trade_date: str):
    eligible = []
    fallback = []
    for task in tasks:
        cached_rows = task[3] if len(task) > 3 else None
        if cached_rows and get_latest_row_trade_date(cached_rows) == previous_trade_date:
            eligible.append(task)
        else:
            fallback.append(task)
    return eligible, fallback


def enrich_spot_row_with_estimated_turnover(
    row: dict,
    cached_rows: list[dict] | None,
) -> dict | None:
    enriched = dict(row)
    if safe_float(enriched.get("turn")) > 0:
        enriched["turn_source"] = "snapshot"
        return enriched

    current_amount = safe_float(enriched.get("amount"))
    current_close = safe_float(enriched.get("close"))
    if current_amount <= 0 or current_close <= 0:
        return None

    for previous in reversed(cached_rows or []):
        previous_amount = safe_float(previous.get("amount"))
        previous_turnover = safe_float(previous.get("turn"))
        previous_close = safe_float(previous.get("close"))
        if previous_amount <= 0 or previous_turnover <= 0 or previous_close <= 0:
            continue
        previous_circ_mv = estimate_circ_mv(previous_amount, previous_turnover)
        current_circ_mv = previous_circ_mv * current_close / previous_close
        if current_circ_mv <= 0:
            continue
        estimated_turnover = current_amount * 100 / current_circ_mv
        if estimated_turnover <= 0:
            continue
        enriched["turn"] = str(round(estimated_turnover, 6))
        enriched["turn_source"] = "estimated_from_previous_circ_mv"
        return enriched

    return None


def convert_akshare_index_history_rows(df, code: str, start_date: str, end_date: str):
    rows = []
    previous_close = None
    for _, row in df.iterrows():
        date_value = akshare_row_value(row, "date", "日期")
        date_str = normalize_date_str(str(date_value).split()[0])
        if not (start_date <= date_str <= end_date):
            continue

        close = safe_float(akshare_row_value(row, "close", "收盘"))
        if previous_close is None:
            previous_close = close
        pct_chg = (
            (close - previous_close) / previous_close * 100
            if previous_close > 0
            else 0
        )
        raw_volume = akshare_row_value(row, "volume", "成交量", default=None)
        if raw_volume is None and "amount" in row:
            # 腾讯指数日线的 amount 字段实际是成交手数。
            raw_volume = safe_float(row.get("amount")) * 100
        rows.append(
            {
                "date": hyphen_date(date_str),
                "code": code,
                "open": str(safe_float(akshare_row_value(row, "open", "开盘"))),
                "high": str(safe_float(akshare_row_value(row, "high", "最高"))),
                "low": str(safe_float(akshare_row_value(row, "low", "最低"))),
                "close": str(close),
                "preclose": str(previous_close),
                "volume": str(safe_float(raw_volume)),
                "amount": "0",
                "turn": "0",
                "pctChg": str(pct_chg),
                "isST": "0",
            }
        )
        previous_close = close
    return rows


def fetch_stock_history_akshare(task):
    code, start_date, end_date = task[:3]
    symbol = f"{code.split('.')[0]}{plain_code(code)}"
    try:
        df = call_akshare(
            ak.stock_zh_a_daily,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            adjust="",
            context=f"{code}新浪日线",
        )
    except Exception as sina_exc:
        try:
            df = call_akshare(
                ak.stock_zh_a_hist_tx,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                adjust="",
                context=f"{code}腾讯日线",
            )
        except Exception:
            plain_symbol = plain_code(code)
            print(f"  ⚠️ {code} 新浪/腾讯日线失败，尝试东财日线: {sina_exc}")
            df = call_akshare(
                ak.stock_zh_a_hist,
                symbol=plain_symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
                context=f"{code}东财日线",
            )
    return code, convert_akshare_stock_history_rows(df, code)


def fetch_index_history_akshare(task):
    code, start_date, end_date = task[:3]
    symbol = f"{code.split('.')[0]}{plain_code(code)}"
    rows = []
    try:
        df = call_akshare(ak.stock_zh_index_daily, symbol=symbol, context=f"{code}新浪指数日线")
        rows = convert_akshare_index_history_rows(df, code, start_date, end_date)
    except Exception as exc:
        print(f"  ⚠️ {code} 新浪指数日线失败，尝试腾讯: {exc}")

    latest_date = get_latest_row_trade_date(rows)
    if latest_date != end_date:
        print(f"  ⚠️ {code} 新浪指数最新 {latest_date or '无'}，切换腾讯补齐 {end_date}")
        tx_df = call_akshare(
            ak.stock_zh_index_daily_tx,
            symbol=symbol,
            context=f"{code}腾讯指数日线",
        )
        tx_rows = convert_akshare_index_history_rows(tx_df, code, start_date, end_date)
        rows = merge_history_rows(rows, tx_rows)
    return code, rows


def fetch_history_akshare(task):
    code = task[0]
    if code in {benchmark_code for benchmark_code, _ in INDEX_BENCHMARKS}:
        return fetch_index_history_akshare(task)
    return fetch_stock_history_akshare(task)


def init_baostock_worker():
    login_baostock(context="worker")
    atexit.register(logout_baostock)


def fetch_stock_history_worker(task):
    code, start_date, end_date = task[:3]
    rs = execute_baostock_query(
        bs.query_history_k_data_plus,
        code,
        HISTORY_FIELDS,
        start_date=hyphen_date(start_date),
        end_date=hyphen_date(end_date),
        frequency="d",
        adjustflag="3",
    )
    rows = resultset_to_dicts(rs)
    return code, rows


def fetch_stock_history_direct(task):
    code, rows = fetch_stock_history_worker(task)
    return code, rows


def process_history_result(
    histories,
    code: str,
    rows,
    start_date: str,
    end_date: str,
    cached_rows=None,
):
    merged_rows = merge_history_rows(cached_rows, rows)
    save_cached_history(code, merged_rows)
    histories[code] = subset_history(merged_rows, start_date, end_date)


def run_history_tasks_isolated(
    tasks,
    histories,
    start_date: str,
    end_date: str,
    offset: int = 0,
    total: int | None = None,
):
    total_tasks = total or len(tasks)
    for idx, task in enumerate(tasks, start=1):
        code = task[0]
        cached_rows = task[3] if len(task) > 3 else None
        rows = None
        for attempt in range(1, 3):
            executor = ProcessPoolExecutor(
                max_workers=1, initializer=init_baostock_worker
            )
            future = executor.submit(fetch_stock_history_worker, task)
            try:
                code, rows = future.result(timeout=HISTORY_PROGRESS_TIMEOUT)
                break
            except Exception as exc:
                print(
                    f"  ⚠️ {code} 第{attempt}次拉取失败，"
                    f"将{'重试' if attempt == 1 else '跳过'}: {exc}"
                )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        if rows is None:
            histories.setdefault(code, [])
        else:
            process_history_result(
                histories, code, rows, start_date, end_date, cached_rows=cached_rows
            )
        current = offset + idx
        if current % 100 == 0 or current == total_tasks:
            print(f"    已完成 {current}/{total_tasks}")


def run_history_tasks_akshare(tasks, histories, start_date: str, end_date: str, workers: int):
    if workers > 1:
        print("  ℹ️ AkShare 日线接口改用串行模式，避免底层 JS 运行时并发崩溃")

    for completed, task in enumerate(tasks, start=1):
        cached_rows = task[3] if len(task) > 3 else None
        try:
            code, rows = fetch_history_akshare(task)
            process_history_result(
                histories,
                code,
                rows,
                start_date,
                end_date,
                cached_rows=cached_rows,
            )
        except Exception as exc:
            print(f"  ⚠️ {task[0]} AkShare 拉取失败，跳过: {exc}")
            histories.setdefault(task[0], subset_history(cached_rows or [], start_date, end_date))

        if completed % 100 == 0 or completed == len(tasks):
            print(f"    已完成 {completed}/{len(tasks)}")


def get_stock_histories(
    stocks,
    start_date: str,
    end_date: str,
    workers: int,
    prefer_bulk_latest: bool = False,
    refresh_end_date: bool = False,
):
    histories = {}
    tasks = []
    for stock in stocks:
        code = stock["code"]
        cached_rows = load_cached_history(code)
        if cached_rows:
            cached_dates = [normalize_date_str(row["date"]) for row in cached_rows]
            cache_covers_range = (
                cached_dates
                and min(cached_dates) <= start_date
                and max(cached_dates) >= end_date
            )
            if cache_covers_range and not refresh_end_date:
                histories[code] = subset_history(cached_rows, start_date, end_date)
                continue
            if cached_dates and min(cached_dates) <= start_date:
                fetch_start = end_date if refresh_end_date else max(cached_dates)
                tasks.append((code, fetch_start, end_date, cached_rows))
                continue
        tasks.append((code, start_date, end_date, None))

    if tasks and using_akshare() and prefer_bulk_latest:
        original_tasks = tasks
        BULK_RUN_METRICS["attempted"] = len(original_tasks)
        bulk_started_at = time.monotonic()
        try:
            previous_trade_date = get_prev_trade_date(end_date)
            bulk_tasks, tasks = select_bulk_increment_tasks(tasks, previous_trade_date)
            BULK_RUN_METRICS["attempted"] = len(bulk_tasks)
            if bulk_tasks:
                print(f"  📦 批量补充 {end_date} 当日行情: {len(bulk_tasks)}只")
                spot_rows = fetch_bulk_latest_stock_rows_akshare(end_date)
                missing_tasks = []
                for task in bulk_tasks:
                    code = task[0]
                    row = spot_rows.get(code)
                    if not row:
                        missing_tasks.append(task)
                        continue
                    row = enrich_spot_row_with_estimated_turnover(row, task[3])
                    if not row:
                        missing_tasks.append(task)
                        continue
                    process_history_result(
                        histories,
                        code,
                        [row],
                        start_date,
                        end_date,
                        cached_rows=task[3],
                    )
                tasks.extend(missing_tasks)
                BULK_RUN_METRICS["written"] = len(bulk_tasks) - len(missing_tasks)
                BULK_RUN_METRICS["fallback"] = len(missing_tasks)
                print(
                    f"  ✓ 批量行情写入 {len(bulk_tasks) - len(missing_tasks)}只，"
                    f"逐股兜底 {len(missing_tasks)}只"
                )
        except Exception as exc:
            tasks = original_tasks
            BULK_RUN_METRICS["fallback"] = BULK_RUN_METRICS["attempted"]
            BULK_RUN_METRICS["error"] = str(exc)
            print(f"  ⚠️ 批量当日行情失败，回退逐股日线: {exc}")
        finally:
            BULK_RUN_METRICS["duration_seconds"] = round(
                time.monotonic() - bulk_started_at,
                2,
            )

    if tasks:
        if using_akshare():
            print(f"  📥 需从 AkShare 拉取历史日线: {len(tasks)}只")
            run_history_tasks_akshare(tasks, histories, start_date, end_date, workers)
        else:
            print(f"  📥 需从 BaoStock 拉取历史日线: {len(tasks)}只")
            if max(1, workers) == 1:
                run_history_tasks_isolated(tasks, histories, start_date, end_date)
            else:
                max_workers = min(max(1, workers), len(tasks))
                executor = ProcessPoolExecutor(
                    max_workers=max_workers, initializer=init_baostock_worker
                )
                future_map = {
                    executor.submit(fetch_stock_history_worker, task): task for task in tasks
                }
                pending = set(future_map)
                completed = 0
                retry_tasks = []

                try:
                    while pending:
                        done, pending = wait(
                            pending,
                            timeout=HISTORY_PROGRESS_TIMEOUT,
                            return_when=FIRST_COMPLETED,
                        )
                        if not done:
                            stalled_tasks = [future_map[future] for future in pending]
                            print(
                                f"  ⚠️ BaoStock 并发拉取超过 {HISTORY_PROGRESS_TIMEOUT}s 无进展，"
                                f"剩余 {len(stalled_tasks)} 只，切换串行重试"
                            )
                            retry_tasks.extend(stalled_tasks)
                            break

                        for future in done:
                            task = future_map[future]
                            cached_rows = task[3] if len(task) > 3 else None
                            try:
                                code, rows = future.result()
                                process_history_result(
                                    histories,
                                    code,
                                    rows,
                                    start_date,
                                    end_date,
                                    cached_rows=cached_rows,
                                )
                            except Exception as exc:
                                print(f"  ⚠️ {task[0]} 并发拉取失败，稍后串行重试: {exc}")
                                retry_tasks.append(task)
                            completed += 1
                            if completed % 100 == 0 or completed == len(tasks):
                                print(f"    已完成 {completed}/{len(tasks)}")
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)

                if retry_tasks:
                    run_history_tasks_isolated(
                        retry_tasks,
                        histories,
                        start_date,
                        end_date,
                        offset=len(tasks) - len(retry_tasks),
                        total=len(tasks),
                    )

    for stock in stocks:
        histories.setdefault(stock["code"], [])

    return histories


def estimate_circ_mv(amount_yuan: float, turnover_ratio_pct: float) -> float:
    if turnover_ratio_pct <= 0:
        return 0.0
    return amount_yuan * 100 / turnover_ratio_pct


def select_momentum_pool(stocks_with_return):
    pool_size = max(1, ceil(len(stocks_with_return) * MOMENTUM_TOP_RATIO))
    return stocks_with_return[:pool_size], pool_size


def compute_momentum_strength_multiplier(avg_return_20d: float) -> float:
    strength = 0.3 + 0.072 * max(avg_return_20d, 0.0)
    return round(min(max(strength, 0.3), 2.1), 4)


def compute_momentum_score(listed_count: int, listed_ratio: float, avg_return_20d: float) -> float:
    strength_multiplier = compute_momentum_strength_multiplier(avg_return_20d)
    # listed_ratio is the internal decimal ratio, e.g. 0.5 means 50%.
    normalized_ratio = min(max(listed_ratio, 0.0), 1.0)
    return round(
        (max(listed_count, 0) ** 0.55)
        * (normalized_ratio ** 1.2)
        * strength_multiplier
        * MOMENTUM_SCORE_SCALE,
        2,
    )


MOMENTUM_STATE_LABELS = {
    "observing": "观察",
    "new_mainline": "新晋主线",
    "building": "主线建立",
    "confirmed": "主线确认",
    "strengthening": "主线增强",
    "climax_warning": "高潮警惕",
    "fading": "主线退潮",
}


def build_momentum_state(
    current_score: float,
    current_rank: int,
    previous_items: list[dict],
) -> dict:
    previous = [
        {
            "momentum_score": safe_float(item.get("momentum_score")),
            "rank": int(item.get("rank") or 999),
        }
        for item in previous_items
        if item
    ][-4:]
    series = previous + [
        {"momentum_score": safe_float(current_score), "rank": int(current_rank or 999)}
    ]

    mainline_days = 0
    for item in reversed(series):
        if item["momentum_score"] < MOMENTUM_MAINLINE_SCORE_MIN:
            break
        mainline_days += 1

    recent = series[-3:]
    baseline = recent[0]
    score_change_3d_pct = (
        round((current_score - baseline["momentum_score"]) / baseline["momentum_score"] * 100, 1)
        if baseline["momentum_score"] > 0
        else 0.0
    )
    rank_change_3d = baseline["rank"] - int(current_rank or 999)
    previous_was_mainline = bool(
        previous and previous[-1]["momentum_score"] >= MOMENTUM_MAINLINE_SCORE_MIN
    )
    recently_was_mainline = any(
        item["momentum_score"] >= MOMENTUM_MAINLINE_SCORE_MIN for item in previous[-3:]
    )

    if current_score >= MOMENTUM_CLIMAX_WARNING_SCORE_MIN:
        state = "climax_warning"
    elif current_score >= MOMENTUM_MAINLINE_SCORE_MIN:
        if not previous_was_mainline:
            state = "new_mainline"
        elif mainline_days >= 3 and score_change_3d_pct >= 10 and rank_change_3d > 0:
            state = "strengthening"
        elif mainline_days >= 3:
            state = "confirmed"
        else:
            state = "building"
    elif recently_was_mainline:
        state = "fading"
    else:
        state = "observing"

    return {
        "momentum_state": state,
        "momentum_state_label": MOMENTUM_STATE_LABELS[state],
        "mainline_days": mainline_days,
        "score_change_3d_pct": score_change_3d_pct,
        "rank_change_3d": rank_change_3d,
        "is_new_mainline": state == "new_mainline",
        "is_confirmed_mainline": state in {
            "confirmed",
            "strengthening",
        },
        "is_fading": state == "fading",
    }


def load_previous_momentum_sector_history(
    trade_date: str,
    window: int = 4,
    data_dir: Path | None = None,
) -> dict:
    root = Path(data_dir) if data_dir is not None else DATA_DIR
    dates = sorted(
        item.name
        for item in root.iterdir()
        if item.is_dir()
        and item.name.isdigit()
        and len(item.name) == 8
        and item.name < trade_date
        and (item / "momentum.json").exists()
    )[-window:]
    history = {}
    for date in dates:
        payload = json.loads(
            (root / date / "momentum.json").read_text(encoding="utf-8")
        )
        if payload.get("model_version") != MOMENTUM_MODEL_VERSION:
            continue
        for sector in payload.get("data", []):
            history.setdefault(sector.get("sector_name"), []).append(
                {
                    "trade_date": date,
                    "momentum_score": sector.get("momentum_score", 0),
                    "rank": sector.get("rank", 999),
                }
            )
    return history


def percentile_score(sorted_values: list[float], value: float) -> float:
    if not sorted_values or value <= 0:
        return 0.0
    position = bisect.bisect_right(sorted_values, value)
    return min(max(position / len(sorted_values), 0.0), 1.0)


def turnover_quality_score(turnover: float) -> float:
    if turnover <= 0:
        return 0.0
    if turnover < 0.5:
        return 0.1
    if turnover < 1.5:
        return 0.35
    if turnover < 4:
        return 0.65
    if turnover <= 12:
        return 1.0
    if turnover <= 20:
        return 0.8
    if turnover <= 30:
        return 0.6
    return 0.35


def build_institution_proxy(history_rows_by_code, stock_codes):
    metrics = {}
    for code in stock_codes:
        rows = history_rows_by_code.get(code, [])
        if not rows:
            continue
        latest = rows[-1]
        amount = safe_float(latest.get("amount"))
        turnover = safe_float(latest.get("turn"))
        circ_mv = estimate_circ_mv(amount, turnover)
        metrics[code] = {
            "amount": amount,
            "turnover": turnover,
            "circ_mv": circ_mv,
            "is_st": str(latest.get("isST", "0")) == "1",
        }

    mv_values = [item["circ_mv"] for item in metrics.values() if item["circ_mv"] > 0]
    amount_values = [item["amount"] for item in metrics.values() if item["amount"] > 0]
    mv_values.sort()
    amount_values.sort()

    institution_scores = {}
    for code, item in metrics.items():
        market_cap_percentile = percentile_score(mv_values, item["circ_mv"])
        amount_percentile = percentile_score(amount_values, item["amount"])
        turnover_quality = turnover_quality_score(item["turnover"])
        capacity_proxy_score = round(
            (
                amount_percentile * 0.5
                + market_cap_percentile * 0.35
                + turnover_quality * 0.15
            )
            * 10,
            2,
        )
        institution_scores[code] = {
            "circ_mv": item["circ_mv"],
            "amount": item["amount"],
            "turnover": item["turnover"],
            "capacity_proxy_score": capacity_proxy_score,
            "amount_percentile_score": round(amount_percentile * 10, 2),
            "market_cap_percentile_score": round(market_cap_percentile * 10, 2),
            "turnover_quality_score": round(turnover_quality * 10, 2),
            "is_st": item["is_st"],
        }

    return institution_scores


def count_consecutive_extreme_days(rows, mode: str) -> int:
    consecutive = 0
    for idx in range(len(rows) - 1, -1, -1):
        # “过去250个交易日”不包含当天，因此参考窗口必须排除当前行。
        if idx < 250:
            break
        window_rows = rows[idx - 250 : idx]
        current_close = safe_float(rows[idx].get("close"))
        if current_close <= 0:
            break

        if mode == "high":
            reference = max(safe_float(row.get("high")) for row in window_rows)
            matched = reference > 0 and current_close >= reference
        else:
            low_values = [
                safe_float(row.get("low"))
                for row in window_rows
                if safe_float(row.get("low")) > 0
            ]
            reference = min(low_values) if low_values else 0
            matched = reference > 0 and current_close <= reference

        if not matched:
            break
        consecutive += 1

    return max(consecutive, 1)


def extract_newhigh_market_snapshot(data):
    if not data:
        return None

    market_stats = data.get("market_stats", {})
    new_high_count = int(market_stats.get("new_high_count", data.get("total_stocks", 0)) or 0)
    new_low_count = int(market_stats.get("new_low_count", 0) or 0)
    total_universe = int(
        market_stats.get("total_universe")
        or data.get("universe_size")
        or 0
    )

    return {
        "trade_date": data.get("trade_date"),
        "new_high_count": new_high_count,
        "new_low_count": new_low_count,
        "net_new_high": new_high_count - new_low_count,
        "total_universe": total_universe,
    }


def build_market_regime_stats(
    trade_date: str,
    total_universe: int,
    new_high_count: int,
    new_low_count: int,
    index_confirmation: dict | None = None,
    depth_trend: dict | None = None,
    recent_snapshots_override: list[dict] | None = None,
):
    current_snapshot = {
        "trade_date": trade_date,
        "new_high_count": new_high_count,
        "new_low_count": new_low_count,
        "net_new_high": new_high_count - new_low_count,
        "total_universe": total_universe,
    }

    if recent_snapshots_override is not None:
        recent_snapshots = [dict(item) for item in recent_snapshots_override if item]
    else:
        recent_snapshots = []
        for date in get_recent_trade_dates(trade_date, window=6):
            if date == trade_date:
                recent_snapshots.append(current_snapshot)
                continue
            snapshot = extract_newhigh_market_snapshot(
                load_local_data(date, "newhigh", fallback_to_latest=False)
            )
            if snapshot:
                recent_snapshots.append(snapshot)

    if not recent_snapshots:
        recent_snapshots = [current_snapshot]

    past_snapshots = [item for item in recent_snapshots if item.get("trade_date") != trade_date]
    past_snapshots = past_snapshots[-5:]
    recent_window = (past_snapshots + [current_snapshot])[-5:]

    recent_high_counts = [item["new_high_count"] for item in past_snapshots]
    recent_net_counts = [item["net_new_high"] for item in past_snapshots]
    avg_5d = round(sum(recent_high_counts) / len(recent_high_counts), 1) if recent_high_counts else float(new_high_count)
    avg_5d_net = round(sum(recent_net_counts) / len(recent_net_counts), 1) if recent_net_counts else float(new_high_count - new_low_count)
    avg_5d_change_pct = (
        round((new_high_count - avg_5d) / avg_5d * 100, 1) if avg_5d > 0 else 0.0
    )

    positive_days_5d = sum(1 for item in recent_window if item["net_new_high"] > 0)
    trend_days = 0
    for item in reversed(recent_window):
        if item["net_new_high"] > 0:
            trend_days += 1
            continue
        break

    new_high_ratio = (
        round(new_high_count / total_universe * 100, 2) if total_universe else 0.0
    )
    new_low_ratio = (
        round(new_low_count / total_universe * 100, 2) if total_universe else 0.0
    )
    net_new_high = new_high_count - new_low_count
    net_new_high_ratio = (
        round(net_new_high / total_universe * 100, 2) if total_universe else 0.0
    )
    if new_low_count == 0:
        new_high_low_ratio = round(float(new_high_count), 2) if new_high_count > 0 else 0.0
    else:
        new_high_low_ratio = round(new_high_count / new_low_count, 2)

    pulse_signal = (
        new_high_ratio >= 1.0
        and net_new_high_ratio >= 0.8
        and new_high_low_ratio >= 3
        and avg_5d_change_pct >= 35
        and trend_days <= 1
    )

    if (
        new_high_ratio >= 1.2
        and net_new_high_ratio >= 0.9
        and trend_days >= 4
        and new_high_low_ratio >= 3
    ):
        breadth_status = "趋势活跃"
        breadth_signal_desc = "新高显著多于新低，且最近几天持续扩散。"
        color_class = "text-red-600"
    elif (
        new_high_ratio >= 0.6
        and net_new_high_ratio >= 0.45
        and trend_days >= 3
        and new_high_low_ratio >= 2
    ):
        breadth_status = "趋势可做"
        breadth_signal_desc = "市场存在持续扩散，但广度强度仍低于全面趋势。"
        color_class = "text-orange-600"
    elif pulse_signal:
        breadth_status = "单日脉冲"
        breadth_signal_desc = "今天新高家数突然放大，但持续性还没建立。"
        color_class = "text-amber-500"
    elif (
        new_high_ratio >= 0.25
        and net_new_high_ratio > 0
        and trend_days >= 2
        and new_high_low_ratio >= 1.2
    ):
        breadth_status = "趋势观察"
        breadth_signal_desc = "市场有局部趋势，但扩散仍不够。"
        color_class = "text-yellow-600"
    else:
        breadth_status = "趋势谨慎"
        breadth_signal_desc = "新高不足或新低压制明显。"
        color_class = "text-gray-500"

    absolute_participation_score = (
        min(new_high_count / 40, 1) * 18 if new_high_count > 0 else 0
    )
    raw_breadth_strength_score = round(
        max(
            0,
            min(
                100,
                new_high_ratio * 20
                + max(net_new_high_ratio, 0) * 24
                + min(new_high_low_ratio, 3) * 1.5
                + min(trend_days, 5) * 2
                + max(min(avg_5d_change_pct, 80), -80) * 0.04
                + absolute_participation_score,
            ),
        )
    )
    breadth_score_caps = {
        "趋势谨慎": 24,
        "单日脉冲": 44,
        "趋势观察": 49,
        "趋势可做": 74,
        "趋势活跃": 100,
    }
    breadth_strength_score = round(
        min(raw_breadth_strength_score, breadth_score_caps[breadth_status])
    )

    depth_trend = depth_trend or {}
    depth_score = round(float(depth_trend.get("score", 0.0) or 0.0), 1)
    depth_status = depth_trend.get("status", "深度不足")
    depth_desc = depth_trend.get("desc", "新高股质量不足。")

    index_confirmation = index_confirmation or {}
    index_score = round(float(index_confirmation.get("score", 0.0) or 0.0), 1)
    index_status = index_confirmation.get("status", "未确认")
    index_summary = index_confirmation.get("summary", "指数未纳入确认。")
    raw_overall_score = round(
        max(0, min(100, breadth_strength_score * 0.55 + depth_score * 0.45))
    )

    status_rank_map = {
        "趋势谨慎": 0,
        "单日脉冲": 1,
        "趋势观察": 1,
        "趋势可做": 2,
        "趋势活跃": 3,
    }
    rank_status_map = {
        0: "趋势谨慎",
        1: "趋势观察",
        2: "趋势可做",
        3: "趋势活跃",
    }
    breadth_rank = status_rank_map[breadth_status]
    depth_rank_map = {
        "深度不足": 0,
        "深度一般": 1,
        "深度可做": 2,
        "深度强": 3,
    }
    depth_rank = depth_rank_map.get(depth_status, 0)
    # 整体趋势是“广度 + 深度”的结果，但不应明显强过广度本身。
    # 对趋势交易来说，广度不足时，深度只能帮助确认或下调，不能把整体环境抬得过于乐观。
    if breadth_rank == 0:
        final_rank = 0
    elif breadth_rank == 1:
        final_rank = 1 if depth_rank >= 1 else 0
    elif breadth_rank == 2:
        final_rank = 2 if depth_rank >= 2 else 1
    else:
        if depth_rank >= 2:
            final_rank = 3 if index_score >= 60 else 2
        elif depth_rank == 1:
            final_rank = 2
        else:
            final_rank = 1

    score_caps = {0: 24, 1: 49, 2: 74, 3: 100}
    strength_score = round(min(raw_overall_score, score_caps[final_rank]))

    market_status = rank_status_map[final_rank]
    market_phase = "单日脉冲" if pulse_signal else market_status
    if final_rank == 3:
        color_class = "text-red-600"
    elif final_rank == 2:
        color_class = "text-orange-600"
    elif final_rank == 1:
        color_class = "text-yellow-600"
    else:
        color_class = "text-gray-500"

    evidence_summary = (
        f"广度状态：{breadth_status}；"
        f"新高 {new_high_count} / 新低 {new_low_count}；"
        f"净新高 {net_new_high}；"
        f"连续扩散 {trend_days} 天；"
        f"深度状态：{depth_status}；"
        f"指数确认：{index_status}。"
    )
    observation_summary = (
        "今日新高家数相对近5日显著放大，但连续扩散天数仍短。"
        if market_phase == "单日脉冲"
        else evidence_summary
    )

    return {
        "new_high_count": new_high_count,
        "new_low_count": new_low_count,
        "net_new_high": net_new_high,
        "new_high_ratio": new_high_ratio,
        "new_low_ratio": new_low_ratio,
        "net_new_high_ratio": net_new_high_ratio,
        "new_high_low_ratio": new_high_low_ratio,
        "avg_5d": avg_5d,
        "avg_5d_net": avg_5d_net,
        "avg_5d_change_pct": avg_5d_change_pct,
        "trend_days": trend_days,
        "positive_days_5d": positive_days_5d,
        "total_universe": total_universe,
        "pulse_signal": pulse_signal,
        "market_breadth_state": breadth_status,
        "market_observation_state": market_phase,
        "market_evidence_summary": evidence_summary,
        "market_observation_summary": observation_summary,
        "market_status": market_status,
        "market_phase": market_phase,
        "market_phase_desc": observation_summary,
        "market_signal": market_status,
        "market_status_desc": evidence_summary,
        "strength_score": strength_score,
        "breadth_strength_score": breadth_strength_score,
        "breadth_status": breadth_status,
        "depth_trend_score": depth_score,
        "depth_evidence_state": depth_status,
        "depth_trend_status": depth_status,
        "depth_trend_desc": depth_desc,
        "overall_trend_score": strength_score,
        "overall_evidence_state": market_status,
        "overall_trend_status": market_status,
        "index_confirmation_score": index_score,
        "index_confirmation_state": index_status,
        "index_confirmation_status": index_status,
        "index_confirmation_summary": index_summary,
        "index_confirmation_details": index_confirmation.get("benchmarks", []),
        "status_color_class": color_class,
    }


def filter_stocks_for_trade_date(stocks, trade_date: str):
    current_date = datetime.strptime(trade_date, "%Y%m%d")
    filtered = []
    for stock in stocks:
        list_date = datetime.strptime(stock["list_date"], "%Y%m%d")
        list_days = (current_date - list_date).days
        if list_days < 20:
            continue
        stock_copy = stock.copy()
        stock_copy["list_days"] = list_days
        filtered.append(stock_copy)
    return filtered


def trim_histories_for_trade_date(history_rows, trade_date: str):
    trimmed = {}
    for code, rows in history_rows.items():
        trimmed[code] = [
            row for row in rows if normalize_date_str(row["date"]) <= trade_date
        ]
    return trimmed


def get_latest_row_trade_date(rows) -> str | None:
    if not rows:
        return None
    latest_date = rows[-1].get("date")
    if not latest_date:
        return None
    return normalize_date_str(latest_date)


def summarize_trade_date_readiness(context, trade_date: str):
    index_latest_dates = {}
    matching_indexes = []
    for code, name in INDEX_BENCHMARKS:
        latest_date = get_latest_row_trade_date(context["index_rows"].get(code, []))
        index_latest_dates[code] = {
            "name": name,
            "latest_date": latest_date,
        }
        if latest_date == trade_date:
            matching_indexes.append(name)

    stock_latest_dates = [
        get_latest_row_trade_date(context["history_rows"].get(stock["code"], []))
        for stock in context.get("stocks", [])
    ]
    stock_match_count = sum(
        1 for latest_date in stock_latest_dates if latest_date == trade_date
    )
    stock_total = len(stock_latest_dates)
    stock_coverage = stock_match_count / stock_total if stock_total else 0.0
    available_stock_dates = [date for date in stock_latest_dates if date]
    most_common_stock_date = (
        Counter(available_stock_dates).most_common(1)[0][0]
        if available_stock_dates
        else None
    )

    return {
        "trade_date": trade_date,
        "matching_indexes": matching_indexes,
        "matching_index_count": len(matching_indexes),
        "index_latest_dates": index_latest_dates,
        "stock_match_count": stock_match_count,
        "stock_total": stock_total,
        "stock_coverage": stock_coverage,
        "most_common_stock_date": most_common_stock_date,
    }


def ensure_trade_date_ready(context, trade_date: str):
    readiness = summarize_trade_date_readiness(context, trade_date)
    required_market_codes = {code for code, _, _, _ in MARKET_DISPLAY_INDEXES}
    stale_market_indexes = [
        item["name"]
        for code, item in readiness["index_latest_dates"].items()
        if code in required_market_codes and item["latest_date"] != trade_date
    ]
    if stale_market_indexes:
        raise RuntimeError(
            f"目标交易日 {trade_date} 的大盘指数未全部就绪: "
            f"{', '.join(stale_market_indexes)}"
        )
    if readiness["matching_index_count"] < MIN_INDEX_DATE_MATCHES:
        latest_index_summary = ", ".join(
            f"{item['name']}={item['latest_date'] or '无数据'}"
            for item in readiness["index_latest_dates"].values()
        )
        raise RuntimeError(
            f"目标交易日 {trade_date} 的指数日线未就绪，"
            f"当前基准指数最新日期: {latest_index_summary}"
        )

    if readiness["stock_coverage"] < MIN_STOCK_DATE_COVERAGE:
        raise RuntimeError(
            f"目标交易日 {trade_date} 的股票日线覆盖不足，"
            f"当前仅 {readiness['stock_match_count']}/{readiness['stock_total']} "
            f"({readiness['stock_coverage']:.1%}) 命中目标日，"
            f"多数股票最新日期为 {readiness['most_common_stock_date'] or '无数据'}"
        )

    print(
        "  ✓ 交易日校验通过: "
        f"指数 {readiness['matching_index_count']}/{len(INDEX_BENCHMARKS)} 命中目标日，"
        f"股票覆盖 {readiness['stock_match_count']}/{readiness['stock_total']} "
        f"({readiness['stock_coverage']:.1%})"
    )
    return readiness


def build_data_quality_metadata(
    trade_date: str,
    readiness: dict,
    allow_intraday: bool = False,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now()
    close_complete = is_market_close_complete(trade_date, now=now)
    return {
        "version": DATA_QUALITY_VERSION,
        "generated_at": now.astimezone().isoformat(timespec="seconds"),
        "market_close_complete": close_complete,
        "snapshot_status": "complete_close" if close_complete else "intraday",
        "intraday_requested": bool(allow_intraday and not close_complete),
        "stock_coverage": round(float(readiness.get("stock_coverage", 0.0)), 4),
        "stock_coverage_pct": round(
            float(readiness.get("stock_coverage", 0.0)) * 100,
            2,
        ),
        "stock_match_count": int(readiness.get("stock_match_count", 0)),
        "stock_total": int(readiness.get("stock_total", 0)),
        "matching_index_count": int(readiness.get("matching_index_count", 0)),
        "required_index_count": len(INDEX_BENCHMARKS),
        "minimum_stock_coverage_pct": round(MIN_STOCK_DATE_COVERAGE * 100, 2),
    }


def prepare_market_context(
    trade_date: str,
    workers: int,
    stock_limit: int | None = None,
    prefer_bulk_latest: bool = False,
):
    stocks, classification, classification_metadata = load_stock_universe(
        trade_date, stock_limit
    )
    full_start_date = (
        datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=400)
    ).strftime("%Y%m%d")
    history_rows = get_stock_histories(
        stocks,
        full_start_date,
        trade_date,
        workers,
        prefer_bulk_latest=prefer_bulk_latest,
        refresh_end_date=(trade_date == datetime.now().strftime("%Y%m%d")),
    )
    index_rows = get_stock_histories(
        [{"code": code, "name": name} for code, name in INDEX_BENCHMARKS],
        full_start_date,
        trade_date,
        min(3, workers),
        refresh_end_date=(trade_date == datetime.now().strftime("%Y%m%d")),
    )
    institution_data = build_institution_proxy(history_rows, [stock["code"] for stock in stocks])
    return {
        "stocks": stocks,
        "classification": classification,
        "classification_metadata": classification_metadata,
        "history_rows": history_rows,
        "index_rows": index_rows,
        "institution_data": institution_data,
    }


def prepare_range_base_context(
    start_date: str, end_date: str, workers: int, stock_limit: int | None = None
):
    stocks, classification, classification_metadata = load_stock_universe(
        end_date, stock_limit
    )
    full_start_date = (
        datetime.strptime(start_date, "%Y%m%d") - timedelta(days=400)
    ).strftime("%Y%m%d")
    history_rows = get_stock_histories(stocks, full_start_date, end_date, workers)
    index_rows = get_stock_histories(
        [{"code": code, "name": name} for code, name in INDEX_BENCHMARKS],
        full_start_date,
        end_date,
        min(3, workers),
    )
    return {
        "stocks": stocks,
        "classification": classification,
        "classification_metadata": classification_metadata,
        "history_rows": history_rows,
        "index_rows": index_rows,
    }


def build_market_context_for_date(base_context, trade_date: str):
    stocks = filter_stocks_for_trade_date(base_context["stocks"], trade_date)
    active_codes = {stock["code"] for stock in stocks}
    history_rows = {
        code: rows
        for code, rows in trim_histories_for_trade_date(
            base_context["history_rows"],
            trade_date,
        ).items()
        if code in active_codes
    }
    index_rows = trim_histories_for_trade_date(base_context["index_rows"], trade_date)
    institution_data = build_institution_proxy(history_rows, [stock["code"] for stock in stocks])
    return {
        "stocks": stocks,
        "classification": base_context["classification"],
        "classification_metadata": base_context["classification_metadata"],
        "history_rows": history_rows,
        "index_rows": index_rows,
        "institution_data": institution_data,
    }


def build_newhigh_sector_confirmation_score(
    new_high_count: int,
    sector_total_count: int,
    avg_consecutive_days: float,
    avg_change_pct: float,
    avg_break_pct: float,
    avg_capacity_proxy_score: float,
    momentum_rank: int | None,
    momentum_mainline: bool,
    momentum_warning: bool,
) -> float:
    breadth_ratio = (
        new_high_count / sector_total_count * 100 if sector_total_count > 0 else 0.0
    )
    sample_confidence = min(math.sqrt(max(new_high_count, 0) / 3), 1)
    count_score = min(math.sqrt(max(new_high_count, 0)) * 12, 30)
    breadth_score = min(max(breadth_ratio, 0) * 1.5, 25)

    if momentum_warning:
        momentum_score = 8
    elif momentum_mainline:
        momentum_score = 28
    elif momentum_rank and momentum_rank <= 12:
        momentum_score = 20
    elif momentum_rank:
        momentum_score = min(max(13 - momentum_rank, 0) * 1.2, 12)
    else:
        momentum_score = 0

    quality_score = (
        min(max(avg_consecutive_days, 0), 3) * 3
        + min(max(avg_change_pct, 0), 8) * 0.8
        + min(max(avg_break_pct, 0), 5) * 0.6
        + min(max(avg_capacity_proxy_score, 0), 10) * 0.3
    )
    evidence_score = (count_score + breadth_score + quality_score) * sample_confidence
    warning_penalty = 12 if momentum_warning else 0
    return round(
        max(
            0,
            min(
                100,
                evidence_score + momentum_score - warning_penalty,
            ),
        ),
        2,
    )


def classify_newhigh_confirmation_tier(
    new_high_count: int,
    momentum_rank: int | None,
    momentum_mainline: bool,
    momentum_warning: bool,
) -> str:
    if momentum_warning:
        return "climax_warning"
    if new_high_count < 2:
        return "observation"
    if momentum_mainline:
        return "mainline_confirmed"
    if momentum_rank and momentum_rank <= 12:
        return "front_runner"
    return "observation"


def build_index_confirmation(index_rows_by_code):
    benchmarks = []
    for code, name in INDEX_BENCHMARKS:
        rows = index_rows_by_code.get(code, [])
        if len(rows) < 60:
            continue

        close = safe_float(rows[-1].get("close"))
        prev_close = safe_float(rows[-2].get("close")) if len(rows) >= 2 else close
        close_20d_ago = safe_float(rows[-21].get("close")) if len(rows) >= 21 else 0
        ma20 = moving_average(rows, 20)
        ma60 = moving_average(rows, 60)
        ma20_prev5 = moving_average(rows, 20, offset=5)
        daily_change_pct = (
            round((close / prev_close - 1) * 100, 2) if close > 0 and prev_close > 0 else 0.0
        )
        return_20d = (
            round((close / close_20d_ago - 1) * 100, 2)
            if close > 0 and close_20d_ago > 0
            else 0.0
        )

        score = 0
        if close > ma20 > 0:
            score += 20
        if close > ma60 > 0:
            score += 30
        if ma20 > ma60 > 0:
            score += 30
        if ma20 > ma20_prev5 > 0:
            score += 10
        if return_20d > 0:
            score += 10

        if score >= 80:
            status = "强确认"
        elif score >= 60:
            status = "确认"
        elif score >= 40:
            status = "弱确认"
        else:
            status = "未确认"

        benchmarks.append(
            {
                "code": code,
                "name": name,
                "close": round(close, 2),
                "daily_change_pct": daily_change_pct,
                "return_20d": return_20d,
                "ma20": round(ma20, 2),
                "ma60": round(ma60, 2),
                "score": score,
                "status": status,
                "above_ma20": close > ma20 > 0,
                "above_ma60": close > ma60 > 0,
                "ma20_above_ma60": ma20 > ma60 > 0,
                "ma20_rising": ma20 > ma20_prev5 > 0,
            }
        )

    if not benchmarks:
        return {
            "score": 0.0,
            "status": "未确认",
            "summary": "指数确认不可用，当前只按市场广度判断。",
            "benchmarks": [],
        }

    aggregate_score = round(
        sum(item["score"] for item in benchmarks) / len(benchmarks), 1
    )
    if aggregate_score >= 80:
        aggregate_status = "强确认"
        summary = "多数核心指数都站上中期均线，指数趋势对广度信号形成强确认。"
    elif aggregate_score >= 60:
        aggregate_status = "确认"
        summary = "指数整体站位偏正，能对广度改善提供一定确认。"
    elif aggregate_score >= 40:
        aggregate_status = "弱确认"
        summary = "指数有修复但还不够顺，广度信号需要谨慎对待。"
    else:
        aggregate_status = "未确认"
        summary = "指数没有跟上，广度信号更像结构性修复，不宜过度放大。"

    return {
        "score": aggregate_score,
        "status": aggregate_status,
        "summary": summary,
        "benchmarks": benchmarks,
    }


def percent_change(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return round((current / previous - 1) * 100, 2)


def stock_matches_market_scope(code: str, scope: str) -> bool:
    normalized = str(code or "").lower()
    plain = plain_code(normalized)
    if scope == "all_market":
        return normalized.startswith("sh.") or normalized.startswith("sz.")
    if scope == "shanghai":
        return normalized.startswith("sh.")
    if scope == "chinext":
        return normalized.startswith("sz.") and plain.startswith(("300", "301"))
    if scope == "star":
        return normalized.startswith("sh.") and plain.startswith(("688", "689"))
    return False


def aggregate_market_scope(history_rows_by_code, dates: list[str], scope: str) -> dict[str, dict]:
    target_dates = set(dates)
    totals = {
        date: {"trade_date": date, "amount": 0.0, "volume": 0.0, "stock_count": 0}
        for date in dates
    }
    for code, rows in history_rows_by_code.items():
        if not stock_matches_market_scope(code, scope):
            continue
        for row in reversed(rows):
            date = normalize_date_str(str(row.get("date") or ""))
            if date not in target_dates:
                if date and date < dates[0]:
                    break
                continue
            close = safe_float(row.get("close"))
            if close <= 0:
                continue
            totals[date]["amount"] += safe_float(row.get("amount"))
            totals[date]["volume"] += safe_float(row.get("volume"))
            totals[date]["stock_count"] += 1

    for item in totals.values():
        item["amount"] = round(item["amount"], 2)
        item["volume"] = round(item["volume"], 2)
    return totals


def get_market_trade_dates(index_rows_by_code, trade_date: str, count: int = 6) -> list[str]:
    rows = index_rows_by_code.get("sh.000001", [])
    dates = [
        normalize_date_str(str(row.get("date") or ""))
        for row in rows
        if row.get("date") and normalize_date_str(str(row["date"])) <= trade_date
    ]
    return dates[-count:]


def get_index_row(index_rows_by_code, code: str, trade_date: str) -> tuple[dict | None, dict | None]:
    rows = [
        row
        for row in index_rows_by_code.get(code, [])
        if normalize_date_str(str(row.get("date") or "")) <= trade_date
    ]
    if not rows:
        return None, None
    current = next(
        (
            row for row in reversed(rows)
            if normalize_date_str(str(row.get("date") or "")) == trade_date
        ),
        None,
    )
    previous_rows = [
        row for row in rows
        if normalize_date_str(str(row.get("date") or "")) < trade_date
    ]
    previous = previous_rows[-1] if previous_rows else None
    return current, previous


def market_change_label(change_pct: float) -> str:
    if change_pct >= 5:
        return "放量"
    if change_pct <= -5:
        return "缩量"
    return "量能平稳"


def build_market_overview(
    trade_date: str,
    history_rows_by_code: dict,
    index_rows_by_code: dict,
    market_stats: dict | None = None,
    momentum_data: dict | None = None,
) -> dict:
    dates = get_market_trade_dates(index_rows_by_code, trade_date, count=6)
    if trade_date not in dates:
        dates.append(trade_date)
    dates = sorted(set(dates))[-6:]

    scope_totals = {
        scope: aggregate_market_scope(history_rows_by_code, dates, scope)
        for scope in ("all_market", "shanghai", "chinext", "star")
    }
    all_market_series = [scope_totals["all_market"][date] for date in dates]
    current_total = all_market_series[-1] if all_market_series else {
        "amount": 0.0,
        "volume": 0.0,
        "stock_count": 0,
    }
    previous_total = all_market_series[-2] if len(all_market_series) >= 2 else current_total
    expected_market_count = sum(
        1
        for code in history_rows_by_code
        if stock_matches_market_scope(code, "all_market")
    )
    current_market_coverage = (
        current_total["stock_count"] / expected_market_count
        if expected_market_count
        else 0.0
    )
    previous_market_coverage = (
        previous_total["stock_count"] / expected_market_count
        if expected_market_count
        else 0.0
    )
    comparison_coverage_valid = (
        current_market_coverage >= MIN_STOCK_DATE_COVERAGE
        and previous_market_coverage >= MIN_STOCK_DATE_COVERAGE
        and abs(current_market_coverage - previous_market_coverage) <= 0.02
    )
    past_totals = all_market_series[:-1][-5:]
    avg_amount_5d = (
        sum(item["amount"] for item in past_totals) / len(past_totals)
        if past_totals else current_total["amount"]
    )
    avg_volume_5d = (
        sum(item["volume"] for item in past_totals) / len(past_totals)
        if past_totals else current_total["volume"]
    )

    advance_count = 0
    decline_count = 0
    flat_count = 0
    for code, rows in history_rows_by_code.items():
        if not stock_matches_market_scope(code, "all_market") or not rows:
            continue
        current_row = next(
            (
                row for row in reversed(rows)
                if normalize_date_str(str(row.get("date") or "")) == trade_date
            ),
            None,
        )
        if not current_row or safe_float(current_row.get("close")) <= 0:
            continue
        change = safe_float(current_row.get("pctChg"))
        if change > 0.001:
            advance_count += 1
        elif change < -0.001:
            decline_count += 1
        else:
            flat_count += 1

    indices = []
    index_change_map = {}
    for code, name, scope, turnover_label in MARKET_DISPLAY_INDEXES:
        current_row, previous_row = get_index_row(index_rows_by_code, code, trade_date)
        data_available = current_row is not None
        close = safe_float((current_row or {}).get("close"))
        previous_close = safe_float((previous_row or {}).get("close"))
        daily_change_pct = percent_change(close, previous_close)
        if data_available:
            index_change_map[code] = daily_change_pct

        series = [scope_totals[scope][date] for date in dates]
        current_scope = series[-1] if series else {"amount": 0.0, "volume": 0.0}
        previous_scope = series[-2] if len(series) >= 2 else current_scope
        past_scope = series[:-1][-5:]
        avg_scope_amount = (
            sum(item["amount"] for item in past_scope) / len(past_scope)
            if past_scope else current_scope["amount"]
        )
        avg_scope_volume = (
            sum(item["volume"] for item in past_scope) / len(past_scope)
            if past_scope else current_scope["volume"]
        )
        indices.append(
            {
                "code": code,
                "name": name,
                "turnover_label": turnover_label,
                "data_available": data_available,
                "data_date": trade_date if data_available else None,
                "close": round(close, 2),
                "daily_change_pct": daily_change_pct,
                "amount": current_scope["amount"],
                "volume": current_scope["volume"],
                "amount_change_pct": percent_change(current_scope["amount"], previous_scope["amount"]),
                "volume_change_pct": percent_change(current_scope["volume"], previous_scope["volume"]),
                "amount_vs_5d_pct": percent_change(current_scope["amount"], avg_scope_amount),
                "volume_vs_5d_pct": percent_change(current_scope["volume"], avg_scope_volume),
                "history": series[-5:],
            }
        )

    displayed_changes = list(index_change_map.values())
    positive_indexes = sum(1 for value in displayed_changes if value > 0.1)
    negative_indexes = sum(1 for value in displayed_changes if value < -0.1)
    average_index_change = (
        sum(displayed_changes) / len(displayed_changes) if displayed_changes else 0.0
    )
    if positive_indexes >= 2 and average_index_change >= 0.35:
        daily_market_status = "当日偏强"
    elif negative_indexes >= 2 and average_index_change <= -0.35:
        daily_market_status = "当日偏弱"
    else:
        daily_market_status = "当日震荡"

    shanghai_change = index_change_map.get("sh.000001")
    growth_changes = [
        index_change_map.get("sz.399006"),
        index_change_map.get("sh.000688"),
    ]
    if shanghai_change is None or any(value is None for value in growth_changes):
        style_status = "数据待更新"
        style_desc = "部分指数尚未更新到当前交易日，暂不判断市场风格。"
    else:
        growth_average = sum(growth_changes) / len(growth_changes)
        growth_spread = growth_average - shanghai_change
        if growth_spread >= 0.8:
            style_status = "成长占优"
            style_desc = "创业板和科创50明显强于上证，资金偏向成长方向。"
        elif growth_spread <= -0.8:
            style_status = "主板占优"
            style_desc = "创业板和科创50弱于上证，成长方向承压更重。"
        else:
            style_status = "风格均衡"
            style_desc = "主板与成长指数差异不大，暂未形成明显风格偏向。"

    amount_change_pct = percent_change(current_total["amount"], previous_total["amount"])
    volume_change_pct = percent_change(current_total["volume"], previous_total["volume"])
    amount_vs_5d_pct = percent_change(current_total["amount"], avg_amount_5d)
    volume_vs_5d_pct = percent_change(current_total["volume"], avg_volume_5d)
    volume_tone = market_change_label(amount_change_pct)
    direction_text = (
        "上涨" if shanghai_change is not None and shanghai_change > 0.1
        else "下跌" if shanghai_change is not None and shanghai_change < -0.1
        else "震荡"
    )
    price_volume_status = f"{volume_tone}{direction_text}"

    market_stats = market_stats or {}
    new_high_count = int(market_stats.get("new_high_count", 0) or 0)
    new_low_count = int(market_stats.get("new_low_count", 0) or 0)
    market_phase = market_stats.get("market_phase", "趋势观察")
    top_sector = ((momentum_data or {}).get("data") or [{}])[0].get("sector_name", "暂无")

    if (
        daily_market_status == "当日偏弱"
        or decline_count > advance_count * 1.5
        or market_phase == "趋势谨慎"
    ):
        operation_tone = "谨慎观察"
        participation_level = "观望"
    elif (
        market_phase in {"趋势可做", "趋势活跃"}
        and daily_market_status == "当日偏强"
        and advance_count > decline_count
        and new_high_count > new_low_count
    ):
        operation_tone = "积极参与"
        participation_level = "积极参与"
    elif market_phase == "趋势观察" or daily_market_status == "当日偏弱":
        operation_tone = "轻仓试错"
        participation_level = "轻仓试错"
    else:
        operation_tone = "正常参与"
        participation_level = "正常参与"

    if daily_market_status == "当日偏弱" and advance_count > decline_count * 1.2:
        market_structure = "指数走弱，个股修复"
    elif daily_market_status == "当日偏强" and decline_count > advance_count * 1.2:
        market_structure = "指数走强，个股承压"
    elif daily_market_status == "当日偏强" and advance_count > decline_count:
        market_structure = "指数个股共振走强"
    elif daily_market_status == "当日偏弱" and decline_count > advance_count:
        market_structure = "指数个股同步走弱"
    else:
        market_structure = "指数个股震荡分化"

    headline = f"{market_structure}，{participation_level}"
    shanghai_change_text = (
        format_signed_percent(shanghai_change)
        if shanghai_change is not None else "暂缺"
    )
    price_volume_analysis = (
        f"上证指数{shanghai_change_text}，沪深两市成交额较昨日"
        f"{format_signed_percent(amount_change_pct)}，成交量较昨日{format_signed_percent(volume_change_pct)}，"
        f"属于{price_volume_status}。"
    )
    if daily_market_status == "当日偏弱" and advance_count > decline_count:
        breadth_prefix = "指数偏弱但个股涨多跌少，市场呈现明显结构性分化。"
    elif daily_market_status == "当日偏强" and decline_count > advance_count:
        breadth_prefix = "指数偏强但个股跌多涨少，权重与多数个股表现分化。"
    else:
        breadth_prefix = "指数与个股方向基本一致。"
    breadth_analysis = (
        f"{breadth_prefix}全市场上涨 {advance_count} 家、下跌 {decline_count} 家；"
        f"250日新高 {new_high_count} 家、新低 {new_low_count} 家。"
    )
    structure_analysis = f"{style_desc} 当前动量最强方向是{top_sector}。"
    if daily_market_status == "当日偏弱":
        watch_text = "关注量能能否收缩、成长指数能否止跌，以及新高家数能否重新扩散。"
    elif daily_market_status == "当日偏强":
        watch_text = "关注放量能否延续、上涨家数能否保持优势，以及强势板块是否继续扩散。"
    else:
        watch_text = "关注三大指数能否形成同向、成交额是否放大，以及新高新低能否转强。"

    return {
        "trade_date": trade_date,
        "market_status": market_phase,
        "daily_market_status": daily_market_status,
        "operation_tone": operation_tone,
        "participation_level": participation_level,
        "market_structure": market_structure,
        "price_volume_status": price_volume_status,
        "style_status": style_status,
        "headline": headline,
        "headline_summary": (
            f"{daily_market_status}；上证指数{shanghai_change_text}，两市成交额较昨日"
            f"{format_signed_percent(amount_change_pct)}；上涨 {advance_count} 家，下跌 {decline_count} 家。"
        ),
        "analysis_summary": f"{price_volume_analysis}{structure_analysis}{breadth_analysis}",
        "watch_text": watch_text,
        "total_market": {
            "amount": current_total["amount"],
            "volume": current_total["volume"],
            "amount_change_pct": amount_change_pct,
            "volume_change_pct": volume_change_pct,
            "amount_vs_5d_pct": amount_vs_5d_pct,
            "volume_vs_5d_pct": volume_vs_5d_pct,
            "advance_count": advance_count,
            "decline_count": decline_count,
            "flat_count": flat_count,
            "stock_count": current_total["stock_count"],
            "expected_stock_count": expected_market_count,
            "stock_coverage_pct": round(current_market_coverage * 100, 2),
            "previous_stock_coverage_pct": round(previous_market_coverage * 100, 2),
            "comparison_coverage_valid": comparison_coverage_valid,
            "history": all_market_series[-5:],
        },
        "indices": indices,
    }


def format_signed_percent(value: float) -> str:
    return f"{value:+.2f}%"


def build_depth_trend_stats(sectors, total_new_high_count: int):
    if not sectors or total_new_high_count <= 0:
        return {
            "score": 0,
            "status": "深度不足",
            "desc": "新高股强度、连续性和容量特征都不足。",
            "avg_change_pct": 0.0,
            "avg_consecutive_days": 0.0,
            "avg_capacity_proxy_score": 0.0,
            "mainline_ratio": 0.0,
            "warning_ratio": 0.0,
            "top3_concentration": 0.0,
        }

    weighted_change = sum(
        safe_float(item.get("avg_change_pct")) * int(item.get("new_high_count") or 0)
        for item in sectors
    ) / total_new_high_count
    weighted_consecutive = sum(
        safe_float(item.get("avg_consecutive_days")) * int(item.get("new_high_count") or 0)
        for item in sectors
    ) / total_new_high_count
    weighted_capacity_proxy = sum(
        safe_float(item.get("avg_capacity_proxy_score", item.get("avg_capacity_score", item.get("avg_institution_score")))) * int(item.get("new_high_count") or 0)
        for item in sectors
    ) / total_new_high_count

    mainline_count = sum(
        int(item.get("new_high_count") or 0)
        for item in sectors
        if item.get("momentum_mainline") and not item.get("momentum_warning")
    )
    warning_count = sum(
        int(item.get("new_high_count") or 0) for item in sectors if item.get("momentum_warning")
    )
    sorted_counts = sorted(
        (int(item.get("new_high_count") or 0) for item in sectors), reverse=True
    )
    top3_concentration = round(sum(sorted_counts[:3]) / total_new_high_count * 100, 2)
    mainline_ratio = round(mainline_count / total_new_high_count * 100, 2)
    warning_ratio = round(warning_count / total_new_high_count * 100, 2)

    if weighted_change >= 7:
        change_score = 26
    elif weighted_change >= 5:
        change_score = 21
    elif weighted_change >= 3:
        change_score = 15
    elif weighted_change >= 1.5:
        change_score = 9
    else:
        change_score = 4

    if weighted_consecutive >= 3:
        consecutive_score = 24
    elif weighted_consecutive >= 2:
        consecutive_score = 18
    elif weighted_consecutive >= 1.5:
        consecutive_score = 12
    else:
        consecutive_score = 6

    capacity_component = min(weighted_capacity_proxy / 8, 1) * 20

    if top3_concentration >= 60:
        concentration_score = 10
    elif top3_concentration >= 45:
        concentration_score = 7
    elif top3_concentration >= 30:
        concentration_score = 4
    else:
        concentration_score = 2

    mainline_component = min(mainline_ratio / 50, 1) * 20
    warning_penalty = min(warning_ratio / 50, 1) * 15

    score = round(
        max(
            0,
            min(
                100,
                change_score
                + consecutive_score
                + capacity_component
                + concentration_score
                + mainline_component
                - warning_penalty,
            ),
        )
    )

    if score >= 70:
        status = "深度强"
        desc = "新高股涨幅、连续性和容量特征都较强。"
    elif score >= 52:
        status = "深度可做"
        desc = "新高股质量和容量特征处于较好水平。"
    elif score >= 35:
        status = "深度一般"
        desc = "有一定趋势质量，但强度和集中度仍有限。"
    else:
        status = "深度不足"
        desc = "新高股质量和容量特征都偏弱。"

    return {
        "score": score,
        "status": status,
        "desc": desc,
        "avg_change_pct": round(weighted_change, 2),
        "avg_consecutive_days": round(weighted_consecutive, 2),
        "avg_capacity_proxy_score": round(weighted_capacity_proxy, 2),
        "mainline_ratio": mainline_ratio,
        "warning_ratio": warning_ratio,
        "top3_concentration": top3_concentration,
    }


def update_momentum_data(
    trade_date: str,
    workers: int,
    stock_limit: int | None = None,
    context=None,
    save_result: bool = True,
    enable_mx_enrichment: bool = True,
):
    print(f"\n📊 更新动量模型数据 ({trade_date})...")
    try:
        if context is None:
            context = prepare_market_context(trade_date, workers, stock_limit)

        stocks = context["stocks"]
        classification = context["classification"]
        classification_metadata = context.get("classification_metadata", {})
        history_rows = context["history_rows"]

        print("  📈 计算20日涨幅...")
        stocks_with_return = []
        for stock in stocks:
            rows = history_rows.get(stock["code"], [])
            # 严格按 20 个交易日间隔计算，需要 21 根K线。
            if len(rows) < 21:
                continue

            latest = rows[-1]
            if str(latest.get("isST", "0")) == "1":
                continue

            close_today = safe_float(rows[-1].get("close"))
            close_20d_ago = safe_float(rows[-21].get("close"))
            if close_today <= 0 or close_20d_ago <= 0:
                continue

            stock_copy = stock.copy()
            stock_copy["return_20d"] = round((close_today / close_20d_ago - 1) * 100, 2)
            stock_copy["close_price"] = close_today
            stocks_with_return.append(stock_copy)

        stocks_with_return.sort(key=lambda x: x["return_20d"], reverse=True)
        top_pool, _ = select_momentum_pool(stocks_with_return)
        print(
            f"  ✓ 初选前{int(MOMENTUM_TOP_RATIO * 100)}%强势股: {len(top_pool)}只"
        )

        institution_data = context["institution_data"]

        scoring_stocks = []
        filtered_stocks = []
        for idx, stock in enumerate(top_pool, start=1):
            inst_data = institution_data.get(stock["code"], {})
            if inst_data.get("is_st"):
                continue

            stock["circ_mv"] = inst_data.get("circ_mv", 0)
            stock["amount"] = inst_data.get("amount", 0)
            stock["capacity_proxy_score"] = inst_data.get("capacity_proxy_score", 0)
            stock["rank_in_market"] = idx
            scoring_stocks.append(stock)

            if stock["capacity_proxy_score"] >= INSTITUTION_MIN_SCORE:
                filtered_stocks.append(stock)

        if not scoring_stocks:
            raise RuntimeError("强势股池无数据，请检查行情返回数据")

        industry_map = {}
        for stock in scoring_stocks:
            industry_map.setdefault(stock["industry"] or "其他", []).append(stock)
        candidate_industry_map = {}
        for stock in filtered_stocks:
            candidate_industry_map.setdefault(stock["industry"] or "其他", []).append(stock)

        result = []
        for industry_name, stocks_list in industry_map.items():
            candidate_list = candidate_industry_map.get(industry_name, [])
            total_count = len([s for s in stocks if s.get("industry") == industry_name])
            if total_count == 0:
                total_count = len(stocks_list)

            listed_count = len(stocks_list)
            listed_ratio = listed_count / total_count if total_count else 0
            avg_return = round(
                sum(stock["return_20d"] for stock in stocks_list) / listed_count, 2
            )
            momentum_score = compute_momentum_score(
                listed_count, listed_ratio, avg_return
            )

            result.append(
                {
                    "rank": 0,
                    "sector_name": industry_name,
                    "momentum_score": momentum_score,
                    "listed_count": listed_count,
                    "total_count": total_count,
                    "listed_ratio": round(listed_ratio * 100, 1),
                    "avg_return_20d": avg_return,
                    "tradable_count": len(candidate_list),
                    "rank_change": 0,
                    "is_main_line": momentum_score >= MOMENTUM_MAINLINE_SCORE_MIN,
                    "is_warning": momentum_score >= MOMENTUM_CLIMAX_WARNING_SCORE_MIN,
                    "stocks": [
                        {
                            "rank": i + 1,
                            "code": stock["code"].split(".")[1],
                            "name": stock["name"],
                            "return_20d": stock["return_20d"],
                            "close_price": round(stock["close_price"], 2),
                            "rank_in_market": stock["rank_in_market"],
                            "circ_mv": round(stock["circ_mv"] / 100000000, 2),
                            "amount": round(stock["amount"] / 10000, 2),
                            "capacity_proxy_score": stock["capacity_proxy_score"],
                        }
                        for i, stock in enumerate(candidate_list)
                    ],
                }
            )

        result.sort(key=lambda item: item["momentum_score"], reverse=True)
        prev_rank_map = {}
        try:
            prev_date = get_prev_trade_date(trade_date)
        except Exception as exc:
            prev_date = None
            print(f"  ⚠️ 获取上一交易日失败，rank_change 将置为 0: {exc}")

        if prev_date:
            prev_data = load_local_data(prev_date, "momentum", fallback_to_latest=False)
            if (
                prev_data
                and prev_data.get("model_version") == MOMENTUM_MODEL_VERSION
                and "data" in prev_data
            ):
                for i, sector in enumerate(prev_data["data"], start=1):
                    prev_rank_map[sector["sector_name"]] = i

        previous_sector_history = load_previous_momentum_sector_history(trade_date)
        for i, item in enumerate(result, start=1):
            item["rank"] = i
            if item["sector_name"] in prev_rank_map:
                item["rank_change"] = prev_rank_map[item["sector_name"]] - i
            item.update(
                build_momentum_state(
                    item["momentum_score"],
                    i,
                    previous_sector_history.get(item["sector_name"], []),
                )
            )

        data = {
            "trade_date": trade_date,
            "generated_at": context.get("data_quality", {}).get("generated_at"),
            "data_quality": context.get("data_quality", {}),
            "data_source": data_source_label(),
            "classification": classification,
            **classification_metadata,
            "model_version": MOMENTUM_MODEL_VERSION,
            "score_formula": MOMENTUM_SCORE_FORMULA,
            "mainline_score_min": MOMENTUM_MAINLINE_SCORE_MIN,
            "climax_warning_score_min": MOMENTUM_CLIMAX_WARNING_SCORE_MIN,
            "capacity_filter_scope": "stock_candidates_only",
            "capacity_min_score": INSTITUTION_MIN_SCORE,
            "total_sectors": len(result),
            "total_stocks": len(scoring_stocks),
            "candidate_stocks": len(filtered_stocks),
            "selection_pool_ratio": MOMENTUM_TOP_RATIO,
            "data": result,
        }
        if save_result:
            if enable_mx_enrichment:
                data = enrich_momentum_with_mx(
                    trade_date, data, mx_api_key=_ensure_mx_apikey()
                )
            save_data(trade_date, "momentum", data)
        else:
            print("  ℹ️ 调试模式：未写入动量数据文件")
        save_stock_industry_mapping()
        print(
            f"✅ 动量数据更新完成: {len(result)}个板块, "
            f"{len(scoring_stocks)}只强股, {len(filtered_stocks)}只容量候选"
        )
        return True
    except Exception as exc:
        print(f"❌ 更新动量数据失败: {exc}")
        traceback.print_exc()
        return False


def update_newhigh_data(
    trade_date: str,
    workers: int,
    stock_limit: int | None = None,
    context=None,
    save_result: bool = True,
    enable_mx_enrichment: bool = True,
):
    print(f"\n📈 更新一年新高数据 ({trade_date})...")
    try:
        if context is None:
            context = prepare_market_context(trade_date, workers, stock_limit)

        stocks = context["stocks"]
        classification = context["classification"]
        classification_metadata = context.get("classification_metadata", {})
        history_rows = context["history_rows"]
        index_rows = context["index_rows"]
        institution_data = context["institution_data"]

        all_new_high_stocks = []
        new_high_candidates = []
        raw_new_high_count = 0
        new_low_count = 0
        history_eligible_stocks = 0
        for stock in stocks:
            rows = history_rows.get(stock["code"], [])
            inst_data = institution_data.get(stock["code"], {})
            if inst_data.get("is_st"):
                continue

            # “站上过去250个交易日最高价”要求今天之前已有250个交易日数据。
            if len(rows) < 251:
                continue
            history_eligible_stocks += 1

            recent_rows = rows[-251:]
            reference_rows = recent_rows[:-1]
            current_row = recent_rows[-1]
            high_250 = max(safe_float(row.get("high")) for row in reference_rows)
            low_values = [safe_float(row.get("low")) for row in reference_rows if safe_float(row.get("low")) > 0]
            low_250 = min(low_values) if low_values else 0
            current_close = safe_float(current_row.get("close"))
            current_high = safe_float(current_row.get("high"))
            prev_close = (
                safe_float(reference_rows[-1].get("close"))
                if reference_rows
                else current_close
            )

            if current_close <= 0:
                continue

            if low_250 > 0 and current_close <= low_250:
                new_low_count += 1

            if high_250 <= 0 or current_close < high_250:
                continue

            stock_copy = stock.copy()
            stock_copy["high_250d"] = high_250
            stock_copy["current_close"] = current_close
            stock_copy["current_high"] = current_high
            stock_copy["break_through_pct"] = round(
                (current_close - high_250) / high_250 * 100, 2
            )
            stock_copy["change_pct"] = (
                round((current_close - prev_close) / prev_close * 100, 2)
                if prev_close > 0
                else 0
            )
            stock_copy["consecutive_days"] = count_consecutive_extreme_days(rows, "high")
            stock_copy["circ_mv"] = inst_data.get("circ_mv", 0)
            stock_copy["amount"] = inst_data.get("amount", 0)
            stock_copy["turnover_ratio"] = inst_data.get("turnover", 0)
            stock_copy["capacity_proxy_score"] = inst_data.get("capacity_proxy_score", 0)
            stock_copy["is_3l_candidate"] = (
                stock_copy["capacity_proxy_score"] >= INSTITUTION_MIN_SCORE
            )
            raw_new_high_count += 1
            all_new_high_stocks.append(stock_copy)

            if stock_copy["is_3l_candidate"]:
                new_high_candidates.append(stock_copy)

        industry_map = {}
        for stock in all_new_high_stocks:
            industry_map.setdefault(stock["industry"] or "其他", []).append(stock)

        momentum_lookup = {}
        momentum_data = load_local_data(trade_date, "momentum", fallback_to_latest=False)
        if momentum_data and "data" in momentum_data:
            for item in momentum_data["data"]:
                momentum_lookup[item["sector_name"]] = item

        sectors = []
        for industry_name, stocks_list in industry_map.items():
            sector_stocks = []
            for stock in stocks_list:
                sector_stocks.append(
                    {
                        "code": stock["code"].split(".")[1],
                        "name": stock["name"],
                        "close": stock["current_close"],
                        "price": stock["current_close"],
                        "high_250": stock["high_250d"],
                        "high_250d": stock["high_250d"],
                        "consecutive_new_high_days": stock["consecutive_days"],
                        "consecutive_days": stock["consecutive_days"],
                        "change_pct": stock["change_pct"],
                        "change": stock["change_pct"],
                        "break_through": stock["break_through_pct"],
                        "break_pct": stock["break_through_pct"],
                        "circ_mv": round(stock["circ_mv"] / 100000000, 2),
                        "amount": round(stock["amount"] / 10000, 2),
                        "turnover_ratio": round(stock["turnover_ratio"], 2),
                        "capacity_proxy_score": stock["capacity_proxy_score"],
                        "is_3l_candidate": stock["is_3l_candidate"],
                        "sector": industry_name,
                        "industry": industry_name,
                    }
                )

            avg_consecutive_days = round(
                sum(stock["consecutive_days"] for stock in stocks_list) / len(stocks_list),
                1,
            )
            avg_change_pct = round(
                sum(stock["change_pct"] for stock in stocks_list) / len(stocks_list),
                2,
            )
            avg_break_pct = round(
                sum(stock["break_through_pct"] for stock in stocks_list) / len(stocks_list),
                2,
            )
            avg_capacity_proxy_score = round(
                sum(stock["capacity_proxy_score"] for stock in stocks_list) / len(stocks_list),
                2,
            )
            momentum_item = momentum_lookup.get(industry_name, {})
            momentum_rank = momentum_item.get("rank")
            momentum_mainline = bool(momentum_item.get("is_main_line"))
            momentum_warning = bool(momentum_item.get("is_warning"))
            momentum_state_label = momentum_item.get("momentum_state_label")
            sector_total_count = int(momentum_item.get("total_count") or 0)
            sector_new_high_ratio = round(
                len(stocks_list) / sector_total_count * 100,
                2,
            ) if sector_total_count else 0.0
            confirmation_tier = classify_newhigh_confirmation_tier(
                new_high_count=len(stocks_list),
                momentum_rank=momentum_rank,
                momentum_mainline=momentum_mainline,
                momentum_warning=momentum_warning,
            )
            sector_confirmation_confidence = round(
                min(math.sqrt(len(stocks_list) / 3), 1) * 100,
                1,
            )
            sector_confirmation_score = build_newhigh_sector_confirmation_score(
                new_high_count=len(stocks_list),
                sector_total_count=sector_total_count,
                avg_consecutive_days=avg_consecutive_days,
                avg_change_pct=avg_change_pct,
                avg_break_pct=avg_break_pct,
                avg_capacity_proxy_score=avg_capacity_proxy_score,
                momentum_rank=momentum_rank,
                momentum_mainline=momentum_mainline,
                momentum_warning=momentum_warning,
            )

            sectors.append(
                {
                    "sector_name": industry_name,
                    "new_high_count": len(stocks_list),
                    "stock_count": len(stocks_list),
                    "candidate_count": sum(
                        1 for stock in stocks_list if stock["is_3l_candidate"]
                    ),
                    "sector_total_count": sector_total_count,
                    "sector_new_high_ratio": sector_new_high_ratio,
                    "avg_consecutive_days": avg_consecutive_days,
                    "avg_change_pct": avg_change_pct,
                    "avg_break_pct": avg_break_pct,
                    "avg_capacity_proxy_score": avg_capacity_proxy_score,
                    "sector_confirmation_score": sector_confirmation_score,
                    "sector_confirmation_confidence": sector_confirmation_confidence,
                    "sector_score": sector_confirmation_score,
                    "momentum_rank": momentum_rank,
                    "momentum_mainline": momentum_mainline,
                    "momentum_warning": momentum_warning,
                    "momentum_state_label": momentum_state_label,
                    "confirmation_tier": confirmation_tier,
                    "stocks": sector_stocks,
                }
            )

        depth_trend = build_depth_trend_stats(sectors, len(all_new_high_stocks))
        history_eligible_ratio = round(
            history_eligible_stocks / len(stocks) * 100, 2
        ) if stocks else 0
        effective_universe = history_eligible_stocks or len(stocks)
        market_strength = round(
            raw_new_high_count / effective_universe * 100, 2
        ) if effective_universe else 0
        market_stats = build_market_regime_stats(
            trade_date,
            total_universe=effective_universe,
            new_high_count=raw_new_high_count,
            new_low_count=new_low_count,
            index_confirmation=build_index_confirmation(index_rows),
            depth_trend=depth_trend,
        )
        market_overview = build_market_overview(
            trade_date,
            history_rows,
            index_rows,
            market_stats=market_stats,
            momentum_data=momentum_data,
        )

        data = {
            "trade_date": trade_date,
            "generated_at": context.get("data_quality", {}).get("generated_at"),
            "data_quality": context.get("data_quality", {}),
            "data_source": data_source_label(),
            "classification": classification,
            **classification_metadata,
            "market_strength": market_strength,
            "universe_size": len(stocks),
            "history_window_requirement": 251,
            "history_eligible_stocks": history_eligible_stocks,
            "history_eligible_ratio": history_eligible_ratio,
            "history_window_complete": history_eligible_ratio >= 95,
            "stock_scope": "all_250d_new_highs",
            "candidate_filter_scope": "capacity_proxy_only",
            "candidate_min_score": INSTITUTION_MIN_SCORE,
            "raw_new_high_count": raw_new_high_count,
            "candidate_stocks": len(new_high_candidates),
            "total_stocks": len(all_new_high_stocks),
            "total_sectors": len(sectors),
            "market_stats": market_stats,
            "market_overview": market_overview,
            "sectors": sorted(sectors, key=lambda item: item["sector_confirmation_score"], reverse=True),
        }
        if save_result:
            if enable_mx_enrichment:
                data = enrich_newhigh_with_mx(
                    trade_date, data, mx_api_key=_ensure_mx_apikey()
                )
            save_data(trade_date, "newhigh", data)
        else:
            print("  ℹ️ 调试模式：未写入一年新高数据文件")
        save_stock_industry_mapping()
        print(
            f"✅ 新高数据更新完成: {len(sectors)}个板块, "
            f"{len(all_new_high_stocks)}只全部新高, "
            f"{len(new_high_candidates)}只3L优选"
        )
        return True
    except Exception as exc:
        print(f"❌ 更新新高数据失败: {exc}")
        traceback.print_exc()
        return False


def clean_old_data(days: int = 36500):
    if days >= 36500:
        print("📦 数据保留策略: 永久保存，不删除历史数据")
        return

    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    for item in DATA_DIR.iterdir():
        if item.is_dir() and item.name.isdigit() and len(item.name) == 8 and item.name < cutoff_date:
            for child in item.glob("*"):
                child.unlink()
            item.rmdir()
            print(f"  已删除旧数据: {item.name}")


def should_enrich_with_mx(date_count: int, skip_requested: bool) -> bool:
    return date_count == 1 and not skip_requested


def build_update_run_status(
    trade_dates: list[str],
    success_count: int,
    duration_seconds: float,
    bulk_attempted: int,
    bulk_written: int,
    bulk_fallback: int,
    bulk_source: str,
    bulk_error: str = "",
    bulk_duration_seconds: float | None = None,
) -> dict:
    fallback_ratio = (
        bulk_fallback / bulk_attempted if bulk_attempted > 0 else 0.0
    )
    reasons = []
    if success_count != len(trade_dates):
        reasons.append("部分交易日生成失败")
    if bulk_attempted > 0 and fallback_ratio > UPDATE_DEGRADED_MAX_FALLBACK_RATIO:
        reasons.append(f"批量行情回退 {fallback_ratio * 100:.1f}%")
    if duration_seconds > UPDATE_DEGRADED_MAX_SECONDS:
        reasons.append(f"总耗时 {duration_seconds / 60:.1f} 分钟")
    if bulk_error:
        reasons.append("批量行情接口异常")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_dates": trade_dates,
        "success_count": success_count,
        "duration_seconds": round(duration_seconds, 2),
        "bulk_source": bulk_source,
        "bulk_attempted": bulk_attempted,
        "bulk_written": bulk_written,
        "bulk_fallback": bulk_fallback,
        "bulk_coverage_pct": round((1 - fallback_ratio) * 100, 2),
        "bulk_duration_seconds": round(
            BULK_RUN_METRICS["duration_seconds"]
            if bulk_duration_seconds is None
            else bulk_duration_seconds,
            2,
        ),
        "degraded": bool(reasons),
        "degraded_reasons": reasons,
        "bulk_error": bulk_error,
    }


def main():
    global ACTIVE_DATA_SOURCE

    run_started_at = time.monotonic()
    reset_bulk_run_metrics()

    parser = argparse.ArgumentParser(description="A股数据更新脚本（BaoStock 免费数据版）")
    parser.add_argument("--date", type=str, help="指定日期 (YYYYMMDD)")
    parser.add_argument("--start-date", type=str, help="开始日期 (YYYYMMDD)")
    parser.add_argument("--end-date", type=str, help="结束日期 (YYYYMMDD)")
    parser.add_argument("--keep-days", type=int, default=36500, help="保留数据天数")
    parser.add_argument("--stock-limit", type=int, help="仅处理前 N 只股票，用于调试")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="BaoStock 并发进程数")
    parser.add_argument(
        "--skip-mx-enrichment",
        action="store_true",
        help="跳过非核心的妙想字段增强；历史区间回补默认跳过",
    )
    parser.add_argument(
        "--data-source",
        choices=("akshare", "baostock"),
        default=MARKET_DATA_SOURCE if MARKET_DATA_SOURCE in {"akshare", "baostock"} else "akshare",
        help="行情数据源，默认 akshare",
    )
    parser.add_argument(
        "--allow-intraday",
        action="store_true",
        help="允许生成尚未收盘的当天快照；此类数据不能通过发布校验",
    )
    parser.add_argument(
        "--target-date-only",
        action="store_true",
        help="仅输出当前应发布的最近完整交易日，用于自动更新前置检查",
    )
    args = parser.parse_args()
    ACTIVE_DATA_SOURCE = args.data_source

    if args.target_date_only:
        needs_baostock_session = not using_akshare()
        if needs_baostock_session:
            login_baostock()
        try:
            print(get_trade_date(0))
        finally:
            if needs_baostock_session:
                logout_baostock()
        return

    automatic_latest_run = not (args.date or args.start_date or args.end_date)

    print("=" * 60)
    print("📊 A股数据更新工具")
    print("=" * 60)
    print(f"📅 数据源: {data_source_label()}")
    print(f"🏃 并发进程数: {args.workers}")
    print(f"💾 保留策略: {'永久保存' if args.keep_days >= 36500 else f'保留{args.keep_days}天'}")
    print("=" * 60)

    if not using_akshare():
        login_baostock()
    try:
        if args.date:
            dates = [args.date]
        elif args.start_date and args.end_date:
            dates = get_trade_dates_in_range(args.start_date, args.end_date)
        else:
            dates = [get_trade_date(0, allow_intraday=args.allow_intraday)]

        incomplete_dates = [
            date
            for date in dates
            if not is_market_close_complete(date) and not args.allow_intraday
        ]
        if incomplete_dates:
            raise RuntimeError(
                "以下日期尚未形成完整收盘数据: "
                f"{', '.join(incomplete_dates)}。如只需盘中快照，请显式使用 --allow-intraday"
            )

        print(f"\n📅 更新日期: {dates}")
        save_result = args.stock_limit is None
        enable_mx_enrichment = should_enrich_with_mx(
            len(dates),
            args.skip_mx_enrichment or not automatic_latest_run,
        )
        if not enable_mx_enrichment:
            print("  ℹ️ 本次跳过非核心的妙想字段增强")
        if not save_result:
            print("\n🧪 调试模式：设置了 stock-limit，本次不会覆盖正式数据文件")

        range_base_context = None
        if len(dates) > 1:
            print("\n📦 预加载整段历史数据上下文...")
            range_base_context = prepare_range_base_context(
                dates[0], dates[-1], args.workers, args.stock_limit
            )

        success_count = 0
        for date in dates:
            print(f"\n{'=' * 60}")
            print(f"📅 正在更新: {date}")
            print(f"{'=' * 60}")

            try:
                if range_base_context is not None:
                    context = build_market_context_for_date(range_base_context, date)
                else:
                    context = prepare_market_context(
                        date,
                        args.workers,
                        args.stock_limit,
                        prefer_bulk_latest=(
                            automatic_latest_run
                            and date == datetime.now().strftime("%Y%m%d")
                        ),
                    )
                fallback_count = int(
                    context.get("classification_metadata", {}).get(
                        "classification_fallback_count",
                        0,
                    )
                )
                if fallback_count > 0:
                    raise RuntimeError(
                        f"东方财富冻结分类存在 {fallback_count} 只回退股票，"
                        "请先更新分类快照再生成正式数据"
                    )
                readiness = ensure_trade_date_ready(context, date)
                context["data_quality"] = build_data_quality_metadata(
                    date,
                    readiness,
                    allow_intraday=args.allow_intraday,
                )
                momentum_ok = update_momentum_data(
                    date,
                    args.workers,
                    args.stock_limit,
                    context=context,
                    save_result=save_result,
                    enable_mx_enrichment=enable_mx_enrichment,
                )
                newhigh_ok = update_newhigh_data(
                    date,
                    args.workers,
                    args.stock_limit,
                    context=context,
                    save_result=save_result,
                    enable_mx_enrichment=enable_mx_enrichment,
                )
                if momentum_ok and newhigh_ok:
                    success_count += 1
            except RuntimeError as exc:
                print(f"❌ 日期 {date} 数据未就绪: {exc}")
            except Exception as exc:
                print(f"❌ 日期 {date} 更新失败: {exc}")
                traceback.print_exc()

        print(f"\n🧹 数据保留策略: {'永久保存' if args.keep_days >= 36500 else '清理旧数据'}")
        clean_old_data(args.keep_days)

        print(f"\n{'=' * 60}")
        print("📊 更新结果:")
        print(f"  更新日期数: {len(dates)}")
        print(f"  成功日期数: {success_count}")
        print(f"  所有日期成功: {'✓ 是' if success_count == len(dates) else '✗ 否'}")
        run_status = build_update_run_status(
            trade_dates=dates,
            success_count=success_count,
            duration_seconds=time.monotonic() - run_started_at,
            bulk_attempted=int(BULK_RUN_METRICS["attempted"]),
            bulk_written=int(BULK_RUN_METRICS["written"]),
            bulk_fallback=int(BULK_RUN_METRICS["fallback"]),
            bulk_source=str(BULK_RUN_METRICS["source"]),
            bulk_error=str(BULK_RUN_METRICS["error"]),
            bulk_duration_seconds=float(BULK_RUN_METRICS["duration_seconds"]),
        )
        write_json_atomic(UPDATE_RUN_STATUS_FILE, run_status)
        write_json_atomic(
            SITE_STATUS_FILE,
            {
                "generated_at": run_status["generated_at"],
                "latest_trade_date": max(dates) if success_count else None,
                "success": success_count == len(dates),
                "degraded": run_status["degraded"],
                "degraded_reasons": run_status["degraded_reasons"],
                "market_close_complete": all(
                    is_market_close_complete(date) for date in dates
                ),
            },
        )
        if run_status["degraded"]:
            print(f"  ⚠️ 更新降级: {'；'.join(run_status['degraded_reasons'])}")
        else:
            print(
                f"  ✓ 批量行情覆盖: {run_status['bulk_coverage_pct']}% | "
                f"总耗时: {run_status['duration_seconds']}秒"
            )
        print(f"\n📁 数据保存位置: {DATA_DIR}")
        print("=" * 60)
        return 0 if success_count == len(dates) else 1
    finally:
        if not using_akshare():
            logout_baostock()


if __name__ == "__main__":
    raise SystemExit(main())
