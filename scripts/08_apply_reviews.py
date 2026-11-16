"""Stage 8 - distribute the graded labels into each run folder.

The labels live in ``results/reviews_by_ai.json``, keyed by run folder. This
script copies each run's labels into that run's folder as ``reviews.json``,
which is what stage 6 reads.

It also checks the labels against the run before writing them: every question
must have a label, the number of ``citation_support`` entries must equal the
number of citations in the trace, and a question with an answer must have a
grade. A mismatch is an error, not a warning, because stage 6 raises on it
anyway and a clear message here is easier to act on.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import ROOT, banner, read_json, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Đưa nhãn vào từng thư mục run")
    parser.add_argument(
        "--labels",
        type=Path,
        default=ROOT / "results" / "reviews.json",
        help="file nhãn gốc",
    )
    args = parser.parse_args()

    banner("Stage 8 · Apply labels")

    payload = read_json(args.labels)
    graded = payload["reviews"]
    print(f"nguồn nhãn: {args.labels}")

    for run, reviews in graded.items():
        run_dir = ROOT / "results" / run
        traces = read_json(run_dir / "traces.json")
        by_id = {r["query_id"]: r for r in reviews}
        trace_ids = [t["query_id"] for t in traces]

        missing = [qid for qid in trace_ids if qid not in by_id]
        extra = [qid for qid in by_id if qid not in trace_ids]
        if missing or extra:
            raise SystemExit(f"{run}: thiếu nhãn {missing}, nhãn thừa {extra}")

        for review in reviews:
            trace = next(t for t in traces if t["query_id"] == review["query_id"])
            citations = trace.get("citations", [])
            support = review.get("citation_support", [])
            if len(support) != len(citations):
                raise SystemExit(
                    f"{run}/{review['query_id']}: có {len(citations)} citation nhưng "
                    f"{len(support)} nhãn citation_support"
                )
            if trace.get("answerable", True):
                grade = review.get("answer_grade")
                if grade not in (0, 1, 2):
                    raise SystemExit(
                        f"{run}/{review['query_id']}: answer_grade phải là 0, 1 hoặc 2"
                    )
            else:
                if not isinstance(review.get("abstention_correct"), bool):
                    raise SystemExit(
                        f"{run}/{review['query_id']}: cần abstention_correct là boolean"
                    )

        write_json(run_dir / "reviews.json", reviews)
        print(f"  {run:<22} {len(reviews)} nhãn -> {run_dir / 'reviews.json'}")

    print("\nĐã sẵn sàng chạy stage 6.")


if __name__ == "__main__":
    main()
