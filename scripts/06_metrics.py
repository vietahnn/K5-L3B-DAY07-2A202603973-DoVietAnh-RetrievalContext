"""Stage 6 - aggregate the agent run with the human labels.

Usage:
    python scripts/06_metrics.py --config hybrid --split dev

Reads the traces and the hand-filled labels, and reports the metrics that
BENCHMARK.md asks for: tool use, grounded answer, citation validity,
answer-citation coverage, abstention and false abstention. Provider errors
are counted separately and are not folded into the quality numbers.

Writes:
    results/agent-<config>-<split>/summary.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import ROOT, banner, read_json, write_json

_STARTER = ROOT / "starter"
if str(_STARTER) not in sys.path:
    sys.path.insert(0, str(_STARTER))

from agent_metrics import aggregate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Tổng hợp trace agent + nhãn thủ công")
    parser.add_argument("--config", choices=("dense", "hybrid", "rerank"), default="hybrid")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--run", type=Path, default=None, help="thư mục run (mặc định results/agent-<config>-<split>)")
    parser.add_argument("--reviews", type=Path, default=None, help="file nhãn (mặc định <run>/reviews.json)")
    args = parser.parse_args()

    banner(f"Stage 6 · Metrics · config={args.config} · split={args.split}")

    run_dir = args.run or (ROOT / "results" / f"agent-{args.config}-{args.split}")
    reviews_path = args.reviews or (run_dir / "reviews.json")
    for path in (run_dir / "traces.json", run_dir / "cases.json", reviews_path):
        if not Path(path).exists():
            raise SystemExit(
                f"thiếu file {path}. Điền nhãn tay trước: copy "
                f"{run_dir / 'reviews.template.json'} thành {reviews_path} rồi chấm."
            )

    cases = read_json(run_dir / "cases.json")
    traces = read_json(run_dir / "traces.json")
    reviews = read_json(reviews_path)

    report = {"split": args.split, **aggregate(cases, traces, reviews)}
    write_json(run_dir / "summary.json", report)

    print(f"cases           : {report['cases']}")
    print(f"provider errors : {report['provider_errors']}")
    tool = report["tool_use"]
    print(f"tool use        : {tool['n']}/{tool['N']}")
    grounded = report["grounded_answer"]
    print(
        f"grounded answer : 0={grounded['0']} 1={grounded['1']} 2={grounded['2']} "
        f"(trung bình {grounded['mean']}/2)"
    )
    citation = report["citation_validity"]
    print(f"citation hợp lệ: {citation['n']}/{citation['N']}")
    coverage = report["answer_citation_coverage"]
    print(f"câu có citation: {coverage['n']}/{coverage['N']}")
    abstention = report["abstention"]
    print(f"abstention đúng : {abstention['n']}/{abstention['N']}")
    false_abstention = report["false_abstention"]
    print(f"từ chối nhầm    : {false_abstention['n']}/{false_abstention['N']}")

    print(f"\nĐã ghi {run_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
