#!/usr/bin/env python3
"""Validate generated files before publishing them."""

import argparse
import json
import sys
from pathlib import Path
from statistics import median


BACKEND_DIR = Path(__file__).parent
MODEL_CONFIG = json.loads(
    (BACKEND_DIR / "model_config.json").read_text(encoding="utf-8")
)
CLASSIFICATION_CONFIG = MODEL_CONFIG["classification"]
MOMENTUM_CONFIG = MODEL_CONFIG["momentum"]
CLASSIFICATION_SNAPSHOT = json.loads(
    (BACKEND_DIR / CLASSIFICATION_CONFIG["snapshot_file"]).read_text(encoding="utf-8")
)

EXPECTED_CLASSIFICATION = str(CLASSIFICATION_CONFIG["name"])
EXPECTED_CLASSIFICATION_VERSION = str(CLASSIFICATION_CONFIG["version"])
EXPECTED_CLASSIFICATION_AS_OF = str(CLASSIFICATION_CONFIG["as_of"])
EXPECTED_CLASSIFICATION_HASH = str(CLASSIFICATION_SNAPSHOT["mapping_sha256"])
EXPECTED_MOMENTUM_MODEL_VERSION = str(MODEL_CONFIG["model_version"])
EXPECTED_MAINLINE_SCORE_MIN = float(MOMENTUM_CONFIG["mainline_score_min"])
EXPECTED_CLIMAX_WARNING_SCORE_MIN = float(
    MOMENTUM_CONFIG["climax_warning_score_min"]
)
RECENT_MIN_BASELINE_DATES = 3
RECENT_THRESHOLDS = {
    "total_stocks": 0.03,
    "total_sectors": 0.25,
    "universe_size": 0.02,
}
RECENT_MARKET_AMOUNT_MIN_RATIO = 0.45
RECENT_MARKET_AMOUNT_MAX_RATIO = 2.2


def load_json(path: Path) -> dict:
    if not path.exists():
        raise ValueError(f"缺少数据文件: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_payloads(
    momentum: dict,
    newhigh: dict,
    *,
    require_data_quality: bool = False,
) -> dict:
    trade_date = str(momentum.get("trade_date") or "")
    if len(trade_date) != 8 or not trade_date.isdigit():
        raise ValueError("动量数据日期无效")
    if str(newhigh.get("trade_date") or "") != trade_date:
        raise ValueError("动量和一年新高的数据日期不一致")
    if momentum.get("classification") != EXPECTED_CLASSIFICATION:
        raise ValueError("动量数据不是东方财富行业板块分类")
    if newhigh.get("classification") != EXPECTED_CLASSIFICATION:
        raise ValueError("一年新高数据不是东方财富行业板块分类")
    for label, payload in (("动量", momentum), ("一年新高", newhigh)):
        if payload.get("classification_version") != EXPECTED_CLASSIFICATION_VERSION:
            raise ValueError(f"{label}数据分类版本不一致")
        if payload.get("classification_as_of") != EXPECTED_CLASSIFICATION_AS_OF:
            raise ValueError(f"{label}数据分类日期不一致")
        if payload.get("classification_mapping_hash") != EXPECTED_CLASSIFICATION_HASH:
            raise ValueError(f"{label}数据分类映射指纹不一致")
        if int(payload.get("classification_fallback_count") or 0) > 0:
            raise ValueError(f"{label}数据仍有东方财富分类回退")

        quality = payload.get("data_quality")
        if require_data_quality and not isinstance(quality, dict):
            raise ValueError(f"{label}数据缺少完整性状态")
        if isinstance(quality, dict):
            if int(quality.get("version") or 0) < 1:
                raise ValueError(f"{label}数据完整性版本无效")
            if not quality.get("market_close_complete"):
                raise ValueError(f"{label}数据不是完整收盘快照")
            if float(quality.get("stock_coverage") or 0) < 0.98:
                raise ValueError(f"{label}数据股票覆盖率不足98%")
            if not payload.get("generated_at"):
                raise ValueError(f"{label}数据缺少生成时间")
    if momentum.get("model_version") != EXPECTED_MOMENTUM_MODEL_VERSION:
        raise ValueError("动量模型版本不是V3")
    if float(momentum.get("mainline_score_min") or 0) != EXPECTED_MAINLINE_SCORE_MIN:
        raise ValueError("动量主线阈值不是2.4")
    if (
        float(momentum.get("climax_warning_score_min") or 0)
        != EXPECTED_CLIMAX_WARNING_SCORE_MIN
    ):
        raise ValueError("动量高潮警惕阈值不是5.3")

    total_stocks = int(momentum.get("total_stocks") or 0)
    total_sectors = int(momentum.get("total_sectors") or 0)
    if total_stocks < 100:
        raise ValueError(f"动量股票数量异常: {total_stocks}")
    if total_sectors < 10:
        raise ValueError(f"板块数量异常: {total_sectors}")
    if not isinstance(momentum.get("data"), list) or not momentum["data"]:
        raise ValueError("动量板块列表为空")
    if any(not item.get("momentum_state") for item in momentum["data"]):
        raise ValueError("动量板块缺少持续状态")
    capacity_candidate_stocks = newhigh.get(
        "capacity_candidate_stocks",
        newhigh.get("candidate_stocks"),
    )
    if not isinstance(capacity_candidate_stocks, int) or capacity_candidate_stocks < 0:
        raise ValueError("一年新高容量候选数量格式无效")
    if not isinstance(newhigh.get("sectors"), list):
        raise ValueError("一年新高板块列表格式无效")
    market_overview = newhigh.get("market_overview")
    if not isinstance(market_overview, dict):
        raise ValueError("缺少大盘总览数据")
    if market_overview.get("trade_date") != trade_date:
        raise ValueError("大盘总览数据日期不一致")
    indices = market_overview.get("indices")
    if not isinstance(indices, list) or len(indices) < 3:
        raise ValueError("大盘指数数据不完整")
    stale_indices = [
        str(item.get("name") or "未知指数")
        for item in indices
        if not item.get("data_available") or item.get("data_date") != trade_date
    ]
    if stale_indices:
        raise ValueError(f"大盘指数日期不一致: {', '.join(stale_indices)}")
    total_market = market_overview.get("total_market")
    if not isinstance(total_market, dict) or float(total_market.get("amount") or 0) <= 0:
        raise ValueError("大盘成交额数据无效")
    if require_data_quality:
        if float(total_market.get("stock_coverage_pct") or 0) < 98:
            raise ValueError("大盘成交汇总覆盖率不足98%")
        if not total_market.get("comparison_coverage_valid"):
            raise ValueError("大盘成交额历史比较覆盖不一致")
    sectors = newhigh.get("sectors") or []
    scores = [float(item.get("sector_confirmation_score") or 0) for item in sectors]
    if scores != sorted(scores, reverse=True):
        raise ValueError("一年新高板块未按方向确认分排序")
    if newhigh.get("stock_scope") == "all_250d_new_highs":
        all_new_high_count = int(newhigh.get("total_stocks") or 0)
        market_new_high_count = int(
            (newhigh.get("market_stats") or {}).get("new_high_count") or 0
        )
        sector_new_high_count = sum(
            len(item.get("stocks") or []) for item in sectors
        )
        flagged_capacity_count = sum(
            1
            for item in sectors
            for stock in (item.get("stocks") or [])
            if stock.get(
                "is_capacity_candidate",
                stock.get("is_3l_candidate"),
            ) is True
        )
        if not (
            all_new_high_count
            == market_new_high_count
            == sector_new_high_count
        ):
            raise ValueError(
                "一年新高数量口径不一致: "
                f"总数{all_new_high_count}/市场{market_new_high_count}/行业{sector_new_high_count}"
            )
        if capacity_candidate_stocks != flagged_capacity_count:
            raise ValueError(
                "一年新高容量优选数量不一致: "
                f"汇总{capacity_candidate_stocks}/明细{flagged_capacity_count}"
            )

    return {
        "trade_date": trade_date,
        "total_stocks": total_stocks,
        "total_sectors": total_sectors,
        "market_indices": len(indices),
    }


def validate_data(data_dir: Path, recent_window: int = 0) -> dict:
    momentum = load_json(data_dir / "momentum_latest.json")
    newhigh = load_json(data_dir / "newhigh_latest.json")
    result = validate_payloads(momentum, newhigh, require_data_quality=True)
    if recent_window > 0:
        result.update(
            validate_recent_anomalies(
                data_dir,
                momentum,
                newhigh,
                recent_window,
            )
        )
    return result


def get_selectable_dates(data_dir: Path) -> list[str]:
    dates_path = data_dir / "dates.json"
    if dates_path.exists():
        payload = load_json(dates_path)
        dates = payload.get("dates") if isinstance(payload, dict) else payload
        if not isinstance(dates, list):
            raise ValueError("可选日期索引格式无效")
        return [str(date) for date in dates]

    return sorted(
        (
            path.name
            for path in data_dir.iterdir()
            if path.is_dir()
            and path.name.isdigit()
            and (path / "momentum.json").exists()
            and (path / "newhigh.json").exists()
        ),
        reverse=True,
    )


def extract_recent_metrics(momentum: dict, newhigh: dict) -> dict[str, float]:
    market_overview = newhigh.get("market_overview") or {}
    total_market = market_overview.get("total_market") or {}
    data_quality = newhigh.get("data_quality") or momentum.get("data_quality") or {}
    return {
        "total_stocks": float(momentum.get("total_stocks") or 0),
        "total_sectors": float(momentum.get("total_sectors") or 0),
        "universe_size": float(
            newhigh.get("universe_size")
            or data_quality.get("stock_total")
            or 0
        ),
        "market_amount": float(total_market.get("amount") or 0),
    }


def validate_recent_anomalies(
    data_dir: Path,
    latest_momentum: dict,
    latest_newhigh: dict,
    recent_window: int,
) -> dict:
    latest_date = str(latest_momentum.get("trade_date") or "")
    candidate_dates = sorted(
        (
            trade_date
            for trade_date in get_selectable_dates(data_dir)
            if trade_date < latest_date
        ),
        reverse=True,
    )[: max(int(recent_window), 0)]

    baseline_metrics = []
    for trade_date in candidate_dates:
        momentum_path = data_dir / trade_date / "momentum.json"
        newhigh_path = data_dir / trade_date / "newhigh.json"
        if not momentum_path.exists() or not newhigh_path.exists():
            continue
        baseline_metrics.append(
            extract_recent_metrics(
                load_json(momentum_path),
                load_json(newhigh_path),
            )
        )

    if len(baseline_metrics) < RECENT_MIN_BASELINE_DATES:
        return {
            "recent_baseline_dates": len(baseline_metrics),
            "recent_anomaly_checks": 0,
        }

    latest_metrics = extract_recent_metrics(latest_momentum, latest_newhigh)
    anomalies = []
    check_count = 0
    for metric_name, max_deviation in RECENT_THRESHOLDS.items():
        baseline_values = [
            item[metric_name]
            for item in baseline_metrics
            if item[metric_name] > 0
        ]
        if len(baseline_values) < RECENT_MIN_BASELINE_DATES:
            continue
        baseline = median(baseline_values)
        latest = latest_metrics[metric_name]
        check_count += 1
        deviation = abs(latest - baseline) / baseline if baseline > 0 else 0
        if latest <= 0 or deviation > max_deviation:
            anomalies.append(
                f"{metric_name}={latest:g}，近{len(baseline_values)}日中位数={baseline:g}，"
                f"偏离={deviation:.1%}"
            )

    amount_values = [
        item["market_amount"]
        for item in baseline_metrics
        if item["market_amount"] > 0
    ]
    if len(amount_values) >= RECENT_MIN_BASELINE_DATES:
        amount_baseline = median(amount_values)
        amount_latest = latest_metrics["market_amount"]
        amount_ratio = amount_latest / amount_baseline if amount_baseline > 0 else 0
        check_count += 1
        if not (
            RECENT_MARKET_AMOUNT_MIN_RATIO
            <= amount_ratio
            <= RECENT_MARKET_AMOUNT_MAX_RATIO
        ):
            anomalies.append(
                f"market_amount={amount_latest:g}，近{len(amount_values)}日中位数={amount_baseline:g}，"
                f"比例={amount_ratio:.2f}"
            )

    if anomalies:
        raise ValueError("最近历史对比异常: " + "；".join(anomalies))
    return {
        "recent_baseline_dates": len(baseline_metrics),
        "recent_anomaly_checks": check_count,
    }


def validate_all_data(data_dir: Path, recent_window: int = 0) -> dict:
    dates = get_selectable_dates(data_dir)
    if not dates:
        raise ValueError("没有可校验的历史交易日")

    latest_result = None
    latest_date = max(dates)
    for trade_date in dates:
        try:
            result = validate_payloads(
                load_json(data_dir / trade_date / "momentum.json"),
                load_json(data_dir / trade_date / "newhigh.json"),
                require_data_quality=(trade_date == latest_date),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{trade_date}: {exc}") from exc
        if trade_date == latest_date:
            latest_result = result

    result = {**(latest_result or {}), "validated_dates": len(dates)}
    if recent_window > 0 and latest_result:
        result.update(
            validate_recent_anomalies(
                data_dir,
                load_json(data_dir / latest_date / "momentum.json"),
                load_json(data_dir / latest_date / "newhigh.json"),
                recent_window,
            )
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "data_dir",
        nargs="?",
        type=Path,
        default=Path(__file__).parent / "data",
    )
    parser.add_argument("--all", action="store_true", help="校验所有可选交易日")
    parser.add_argument(
        "--recent-window",
        type=int,
        default=0,
        help="用最近 N 个历史交易日的中位数检查股票池、板块、市场范围和成交额异常",
    )
    args = parser.parse_args()
    try:
        result = (
            validate_all_data(args.data_dir, recent_window=args.recent_window)
            if args.all
            else validate_data(args.data_dir, recent_window=args.recent_window)
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"数据校验失败: {exc}", file=sys.stderr)
        return 1

    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
