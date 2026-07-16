#!/usr/bin/env python3
"""Build selectable-date indexes for generated static data."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent
MODEL_CONFIG = json.loads(
    (BACKEND_DIR / "model_config.json").read_text(encoding="utf-8")
)
EXPECTED_MODEL_VERSION = str(MODEL_CONFIG["model_version"])


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def is_newhigh_complete(data_dir: Path, trade_date: str) -> bool:
    path = data_dir / trade_date / "newhigh.json"
    if not path.exists():
        return False
    payload = load_json(path)
    if "history_window_complete" in payload:
        return bool(payload.get("history_window_complete"))
    return not (
        int(payload.get("total_stocks", 0) or 0) == 0
        and int(payload.get("total_sectors", 0) or 0) == 0
    )


def is_momentum_complete(data_dir: Path, trade_date: str) -> bool:
    path = data_dir / trade_date / "momentum.json"
    if not path.exists():
        return False
    payload = load_json(path)
    sectors = payload.get("data")
    return (
        payload.get("model_version") == EXPECTED_MODEL_VERSION
        and isinstance(sectors, list)
        and bool(sectors)
        and all(item.get("momentum_state") for item in sectors)
    )


def build_date_indexes(data_dir: Path) -> dict:
    all_dates = sorted(
        item.name
        for item in data_dir.iterdir()
        if item.is_dir() and item.name.isdigit() and len(item.name) == 8
    )
    momentum_dates = [
        trade_date
        for trade_date in all_dates
        if is_momentum_complete(data_dir, trade_date)
    ]
    newhigh_dates = [
        trade_date
        for trade_date in all_dates
        if is_newhigh_complete(data_dir, trade_date)
    ]
    newhigh_set = set(newhigh_dates)
    shared_dates = [
        trade_date for trade_date in momentum_dates if trade_date in newhigh_set
    ]

    write_json_atomic(data_dir / "dates.json", {"dates": shared_dates})
    write_json_atomic(
        data_dir / "momentum_dates.json",
        {"dates": momentum_dates},
    )
    write_json_atomic(
        data_dir / "newhigh_dates.json",
        {"dates": newhigh_dates},
    )
    return {
        "shared_dates": len(shared_dates),
        "momentum_dates": len(momentum_dates),
        "newhigh_dates": len(newhigh_dates),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "data_dir",
        nargs="?",
        type=Path,
        default=BACKEND_DIR / "data",
    )
    args = parser.parse_args()
    result = build_date_indexes(args.data_dir)
    print(
        "date_indexes="
        f"shared:{result['shared_dates']},"
        f"momentum:{result['momentum_dates']},"
        f"newhigh:{result['newhigh_dates']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
