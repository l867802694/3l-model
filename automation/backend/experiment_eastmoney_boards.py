#!/usr/bin/env python3
"""
Experiment with Eastmoney industry board classification.

This script is intentionally not wired into update_data.py. It fetches
Eastmoney industry boards and their constituents, then reports whether a stock
maps to one or multiple boards.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RAW_CACHE_ROOT = BASE_DIR / "raw_cache"
OUTPUT_FILE = RAW_CACHE_ROOT / "eastmoney_board_mapping_experiment.json"
EASTMONEY_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}
HTTP_SESSION = requests.Session()
HTTP_SESSION.trust_env = False


def fetch_json(params: dict, retries: int = 3) -> dict:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = HTTP_SESSION.get(
                EASTMONEY_CLIST_URL,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("rc") != 0:
                raise RuntimeError(f"Eastmoney rc={payload.get('rc')}: {payload}")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.8 * attempt)
    raise RuntimeError(f"Eastmoney request failed: {last_error}")


def fetch_industry_boards() -> list[dict]:
    boards = []
    page = 1
    page_size = 100
    total = None

    while total is None or len(boards) < total:
        payload = fetch_json(
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
                "fields": "f12,f14,f3,f20,f21,f62,f128,f136,f152",
            }
        )
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        rows = data.get("diff", []) or []
        if not rows:
            break
        boards.extend(rows)
        page += 1

    return [
        {
            "board_code": item.get("f12"),
            "board_name": item.get("f14"),
            "change_pct": item.get("f3"),
            "market_cap": item.get("f20"),
            "float_market_cap": item.get("f21"),
        }
        for item in boards
        if item.get("f12") and item.get("f14")
    ]


def fetch_board_components(board: dict) -> tuple[dict, list[dict]]:
    payload = fetch_json(
        {
            "pn": 1,
            "pz": 10000,
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
    rows = payload.get("data", {}).get("diff", []) or []
    stocks = [
        {
            "code": str(item.get("f12", "")).zfill(6),
            "name": item.get("f14"),
            "change_pct": item.get("f3"),
            "price": item.get("f2"),
            "amount": item.get("f6"),
            "market_cap": item.get("f20"),
            "float_market_cap": item.get("f21"),
        }
        for item in rows
        if item.get("f12") and item.get("f14")
    ]
    return board, stocks


def build_mapping(boards: list[dict], workers: int) -> tuple[dict, dict]:
    board_components = {}
    stock_to_boards = defaultdict(list)
    max_workers = max(1, min(workers, len(boards)))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_board_components, board) for board in boards]
        for idx, future in enumerate(as_completed(futures), start=1):
            board, stocks = future.result()
            board_components[board["board_code"]] = {
                **board,
                "stock_count": len(stocks),
                "stocks": stocks,
            }
            for stock in stocks:
                stock_to_boards[stock["code"]].append(
                    {
                        "board_code": board["board_code"],
                        "board_name": board["board_name"],
                    }
                )
            if idx % 50 == 0 or idx == len(boards):
                print(f"  fetched board constituents {idx}/{len(boards)}")

    return dict(board_components), dict(stock_to_boards)


def load_latest_momentum_codes() -> set[str]:
    latest_file = DATA_DIR / "momentum_latest.json"
    if not latest_file.exists():
        return set()
    payload = json.loads(latest_file.read_text(encoding="utf-8"))
    codes = set()
    for sector in payload.get("data", []):
        for stock in sector.get("stocks", []):
            code = str(stock.get("code", "")).zfill(6)
            if code:
                codes.add(code)
    return codes


def summarize(board_components: dict, stock_to_boards: dict) -> dict:
    board_sizes = [item["stock_count"] for item in board_components.values()]
    board_count_by_stock = Counter(len(boards) for boards in stock_to_boards.values())
    latest_codes = load_latest_momentum_codes()
    latest_covered = latest_codes & set(stock_to_boards)
    multi_board_codes = [
        code for code, boards in stock_to_boards.items() if len(boards) > 1
    ]
    latest_multi = [code for code in latest_codes if len(stock_to_boards.get(code, [])) > 1]
    max_examples = sorted(
        stock_to_boards.items(), key=lambda item: len(item[1]), reverse=True
    )[:15]

    return {
        "board_count": len(board_components),
        "unique_stock_count": len(stock_to_boards),
        "board_size": {
            "min": min(board_sizes) if board_sizes else 0,
            "max": max(board_sizes) if board_sizes else 0,
            "avg": round(sum(board_sizes) / len(board_sizes), 2) if board_sizes else 0,
        },
        "board_count_by_stock": dict(sorted(board_count_by_stock.items())),
        "multi_board_stock_count": len(multi_board_codes),
        "multi_board_stock_ratio": round(
            len(multi_board_codes) / len(stock_to_boards) * 100, 2
        )
        if stock_to_boards
        else 0,
        "latest_momentum_stock_count": len(latest_codes),
        "latest_momentum_covered_count": len(latest_covered),
        "latest_momentum_covered_ratio": round(
            len(latest_covered) / len(latest_codes) * 100, 2
        )
        if latest_codes
        else 0,
        "latest_momentum_multi_board_count": len(latest_multi),
        "latest_momentum_multi_board_ratio": round(
            len(latest_multi) / len(latest_codes) * 100, 2
        )
        if latest_codes
        else 0,
        "max_board_examples": [
            {
                "code": code,
                "board_count": len(boards),
                "boards": [item["board_name"] for item in boards[:12]],
            }
            for code, boards in max_examples
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-boards", type=int, default=0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    args = parser.parse_args()

    RAW_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    boards = fetch_industry_boards()
    if args.limit_boards:
        boards = boards[: args.limit_boards]
    print(f"Eastmoney industry boards: {len(boards)}")

    board_components, stock_to_boards = build_mapping(boards, args.workers)
    summary = summarize(board_components, stock_to_boards)
    payload = {
        "source": "eastmoney_push2_industry_boards",
        "board_query_fs": "m:90 t:2 f:!50",
        "component_query_fs": "b:{board_code}",
        "summary": summary,
        "boards": board_components,
        "stock_to_boards": stock_to_boards,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
