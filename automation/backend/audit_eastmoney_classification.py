#!/usr/bin/env python3
"""Audit the frozen Eastmoney industry snapshot against current board membership."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from experiment_eastmoney_boards import build_mapping, fetch_industry_boards


BACKEND_DIR = Path(__file__).parent
MODEL_CONFIG = json.loads(
    (BACKEND_DIR / "model_config.json").read_text(encoding="utf-8")
)


def normalize_industry_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"\s*(?:Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ|VI|V|IV|III|II|I)\s*$", "", text)
    return text.strip() or "其他"


def load_snapshot(path: Path | None = None) -> dict:
    snapshot_path = path or BACKEND_DIR / MODEL_CONFIG["classification"]["snapshot_file"]
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


def normalize_memberships(stock_to_boards: dict) -> dict[str, set[str]]:
    return {
        str(code).zfill(6): {
            normalize_industry_name(item.get("board_name"))
            for item in (memberships or [])
            if normalize_industry_name(item.get("board_name")) != "其他"
        }
        for code, memberships in stock_to_boards.items()
    }


def infer_board_parents(
    frozen_mapping: dict[str, str],
    current_memberships: dict[str, set[str]],
    *,
    minimum_overlap: int = 3,
    minimum_confidence: float = 0.65,
) -> tuple[dict[str, str], list[dict]]:
    frozen_industries = set(frozen_mapping.values())
    board_counts: dict[str, Counter] = defaultdict(Counter)
    for code, boards in current_memberships.items():
        frozen_industry = frozen_mapping.get(code)
        if not frozen_industry:
            continue
        for board in boards:
            board_counts[board][frozen_industry] += 1

    inferred = {}
    ambiguous = []
    for board, counts in sorted(board_counts.items()):
        if board in frozen_industries:
            inferred[board] = board
            continue
        parent, parent_count = counts.most_common(1)[0]
        overlap = sum(counts.values())
        confidence = parent_count / overlap if overlap else 0
        if overlap >= minimum_overlap and confidence >= minimum_confidence:
            inferred[board] = parent
        else:
            ambiguous.append(
                {
                    "board_name": board,
                    "overlap": overlap,
                    "best_parent": parent,
                    "confidence": round(confidence, 4),
                    "candidates": dict(counts.most_common(5)),
                }
            )
    return inferred, ambiguous


def compare_classification(
    snapshot: dict,
    stock_to_boards: dict,
    *,
    max_examples: int = 100,
    minimum_parent_overlap: int = 3,
    minimum_parent_confidence: float = 0.65,
) -> dict:
    frozen_mapping = {
        str(code).zfill(6): normalize_industry_name(industry)
        for code, industry in (snapshot.get("mapping") or {}).items()
    }
    current_memberships = normalize_memberships(stock_to_boards)
    board_parents, ambiguous_boards = infer_board_parents(
        frozen_mapping,
        current_memberships,
        minimum_overlap=minimum_parent_overlap,
        minimum_confidence=minimum_parent_confidence,
    )
    frozen_codes = set(frozen_mapping)
    current_codes = set(current_memberships)

    new_codes = sorted(current_codes - frozen_codes)
    absent_codes = sorted(frozen_codes - current_codes)
    changed = []
    unresolved = []
    for code in sorted(frozen_codes & current_codes):
        expected = frozen_mapping[code]
        current = sorted(current_memberships[code])
        inferred_current = sorted(
            {
                board_parents[board]
                for board in current
                if board in board_parents
            }
        )
        if expected in inferred_current:
            continue
        item = {
            "code": code,
            "frozen_industry": expected,
            "current_industries": current,
            "inferred_frozen_industries": inferred_current,
        }
        if inferred_current:
            changed.append(
                item
            )
        else:
            unresolved.append(item)

    new_items = [
        {
            "code": code,
            "current_industries": sorted(current_memberships[code]),
            "inferred_frozen_industries": sorted(
                {
                    board_parents[board]
                    for board in current_memberships[code]
                    if board in board_parents
                }
            ),
        }
        for code in new_codes
    ]
    review_count = len(new_items) + len(changed) + len(unresolved)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "snapshot_version": snapshot.get("classification_version"),
        "snapshot_as_of": snapshot.get("classification_as_of"),
        "snapshot_stock_count": len(frozen_codes),
        "current_board_stock_count": len(current_codes),
        "unchanged_stock_count": len(frozen_codes & current_codes) - len(changed),
        "new_stock_count": len(new_items),
        "changed_industry_count": len(changed),
        "unresolved_stock_count": len(unresolved),
        "absent_from_current_boards_count": len(absent_codes),
        "inferred_board_parent_count": len(board_parents),
        "ambiguous_board_count": len(ambiguous_boards),
        "parent_inference": {
            "minimum_overlap": minimum_parent_overlap,
            "minimum_confidence": minimum_parent_confidence,
        },
        "review_count": review_count,
        "needs_review": review_count > 0,
        "new_stocks": new_items[:max_examples],
        "changed_industries": changed[:max_examples],
        "unresolved_stocks": unresolved[:max_examples],
        "ambiguous_boards": ambiguous_boards[:max_examples],
        "absent_from_current_boards": absent_codes[:max_examples],
        "truncated": {
            "new_stocks": len(new_items) > max_examples,
            "changed_industries": len(changed) > max_examples,
            "unresolved_stocks": len(unresolved) > max_examples,
            "ambiguous_boards": len(ambiguous_boards) > max_examples,
            "absent_from_current_boards": len(absent_codes) > max_examples,
        },
        "policy": "report_only_manual_snapshot_upgrade",
    }


def load_current_memberships(path: Path | None, workers: int) -> tuple[dict, int]:
    if path:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("stock_to_boards") or {}, len(payload.get("boards") or {})

    boards = fetch_industry_boards()
    board_components, stock_to_boards = build_mapping(boards, workers)
    return stock_to_boards, len(board_components)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--current-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("classification-audit.json"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--max-examples", type=int, default=100)
    parser.add_argument("--minimum-parent-overlap", type=int, default=3)
    parser.add_argument("--minimum-parent-confidence", type=float, default=0.65)
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    memberships, board_count = load_current_memberships(
        args.current_file,
        max(1, args.workers),
    )
    report = compare_classification(
        snapshot,
        memberships,
        max_examples=max(1, args.max_examples),
        minimum_parent_overlap=max(1, args.minimum_parent_overlap),
        minimum_parent_confidence=min(
            max(args.minimum_parent_confidence, 0.5),
            1.0,
        ),
    )
    report["current_board_count"] = board_count
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        "东方财富分类检查: "
        f"板块{board_count}个，新增{report['new_stock_count']}只，"
        f"归属变化{report['changed_industry_count']}只，"
        f"待归并{report['unresolved_stock_count']}只，"
        f"待确认{report['review_count']}只"
    )
    print(f"报告: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
