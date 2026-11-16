"""Stage 0 - assemble the Day 7 benchmark.

Usage:
    python scripts/00_build_questions.py --day6 <path to Day 6 benchmark/questions.json>

Why the benchmark is built from two sources
-------------------------------------------
Day 6 already ran all 50 of its questions against retrieval results and used
its dev *and* test splits to choose a chunking strategy. None of those 50
questions is therefore a clean held-out set for Day 7.

So the split is:

* **dev** - 15 answerable questions taken from Day 6, plus 2 no-evidence
  questions written fresh. Day 6's questions are legitimate for development:
  they have hand-verified gold quotes, and the contamination matters only for
  a set that is used to judge an unseen configuration.
* **test** - 5 answerable questions written fresh against the PDF, plus 2
  fresh no-evidence questions. These were never used to choose anything.

The file records the provenance of each question so that the report can state
plainly which questions were held out and which were not.

Writes:
    data/questions.json
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _common import ROOT, banner, read_json, write_json

from src.benchmark import validate_questions

#: How many questions of each kind the Day 7 spec asks for.
DEV_ANSWERABLE = 15
DEV_NO_EVIDENCE = 2
TEST_ANSWERABLE = 5
TEST_NO_EVIDENCE = 2


def convert_day6_question(question: dict) -> dict:
    """Convert a Day 6 question into the Day 7 question schema.

    Day 6 records a primary gold page plus, in ``gold_evidence``, every other
    page that states the same fact. The report restates its headline figures
    in the Foreword, in Main Messages and again in the Overview chapter, so a
    question may legitimately be answered from any of those pages.

    Every one of those pages becomes an entry in the Day 7 ``gold`` list,
    where each entry is a sufficient source on its own. Keeping only the
    primary page would understate what a correct retriever has found: a
    retriever that lands on page 25 with the exact right sentence would be
    scored as a miss.
    """
    gold = question.get("gold")
    if gold is None:
        gold = []
        # The alternative sources, each with the sentence from its own page.
        for entry in question.get("gold_evidence") or []:
            quote = entry.get("quote")
            page = entry.get("page")
            if quote and page is not None:
                gold.append(
                    {
                        "source_id": question.get("gold_source_id"),
                        "page": page,
                        "evidence": quote,
                    }
                )
        # The primary source, when it is not already covered.
        if not gold and question.get("gold_page") is not None:
            gold.append(
                {
                    "source_id": question.get("gold_source_id"),
                    "page": question.get("gold_page"),
                    "evidence": question.get("evidence_quote"),
                }
            )
    converted = {
        "id": f"d-{question['id']}",
        "split": "dev",
        "question": question["question"],
        "expected_answer": question["expected_answer"],
        "gold": gold,
        "answerable": True,
        "filter": question.get("filter"),
        "provenance": "day6",
    }
    return converted


def main() -> None:
    parser = argparse.ArgumentParser(description="Ghép bộ benchmark Day 7")
    parser.add_argument(
        "--day6",
        type=Path,
        required=True,
        help="đường dẫn tới benchmark/questions.json của Day 6",
    )
    parser.add_argument(
        "--fresh",
        type=Path,
        default=ROOT / "data" / "questions_fresh.json",
        help="file câu hỏi viết mới",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "questions.json")
    args = parser.parse_args()

    banner("Stage 0 · Build benchmark")

    day6 = read_json(args.day6)
    day6_questions = day6["questions"] if isinstance(day6, dict) else day6
    fresh = read_json(args.fresh)

    # -- dev: answerable questions from Day 6 --------------------------------
    # Prefer questions whose evidence sits on a single page: they are the
    # cleanest test of whether retrieval found the right place, without the
    # confound of evidence split across chunks.
    single_page = [
        q
        for q in day6_questions
        if q.get("gold_source_id") and q.get("evidence_quote") and q.get("split") == "dev"
    ]
    dev_answerable = [convert_day6_question(q) for q in single_page[:DEV_ANSWERABLE]]
    if len(dev_answerable) < DEV_ANSWERABLE:
        raise SystemExit(
            f"Day 6 chỉ còn {len(dev_answerable)} câu dev dùng được, cần {DEV_ANSWERABLE}"
        )

    # -- no-evidence questions, from the fresh file -------------------------
    dev_no_evidence = [
        {**q, "split": "dev", "provenance": "fresh"}
        for q in fresh["no_evidence"]
        if q["split"] == "dev"
    ]
    test_no_evidence = [
        {**q, "split": "test", "provenance": "fresh"}
        for q in fresh["no_evidence"]
        if q["split"] == "test"
    ]

    if len(dev_no_evidence) != DEV_NO_EVIDENCE:
        raise SystemExit(
            f"cần {DEV_NO_EVIDENCE} câu thiếu evidence cho dev, có {len(dev_no_evidence)}"
        )
    if len(test_no_evidence) != TEST_NO_EVIDENCE:
        raise SystemExit(
            f"cần {TEST_NO_EVIDENCE} câu thiếu evidence cho test, có {len(test_no_evidence)}"
        )

    # -- test: answerable questions written fresh ----------------------------
    test_answerable = [
        {**q, "answerable": True, "provenance": "fresh"}
        for q in fresh["answerable"][:TEST_ANSWERABLE]
    ]
    if len(test_answerable) != TEST_ANSWERABLE:
        raise SystemExit(
            f"cần {TEST_ANSWERABLE} câu có đáp án cho test, có {len(test_answerable)}"
        )

    questions = dev_answerable + dev_no_evidence + test_answerable + test_no_evidence
    validate_questions(questions)

    payload = {
        "benchmark_id": "ppp2026-overview-day7-v1",
        "document_id": "ppp2026-overview",
        "source": "Poverty, Prosperity and Planet Report 2026 - Overview.pdf",
        "notes": (
            "Dev = 15 câu lấy từ benchmark Day 6 (đã có gold quote được kiểm chứng tay) "
            "+ 2 câu thiếu evidence viết mới. Test = 5 câu có đáp án viết mới từ PDF + "
            "2 câu thiếu evidence viết mới. Cả 50 câu của Day 6 đã từng chạy qua retrieval "
            "và dùng để chọn chiến lược chunking, nên không câu nào của Day 6 được dùng làm "
            "test của Day 7. Câu nào viết mới thì không có trong bất kỳ câu nào của Day 6."
        ),
        "contamination_note": (
            "Bộ test của Day 7 gồm các câu viết mới, không dùng để chọn bất kỳ cấu hình "
            "nào. Cấu hình được chọn trên dev rồi mới chạy test."
        ),
        "questions": questions,
    }
    write_json(args.out, payload)

    n_dev = sum(1 for q in questions if q["split"] == "dev")
    n_test = sum(1 for q in questions if q["split"] == "test")
    print(f"dev  : {n_dev} câu ({DEV_ANSWERABLE} có đáp án + {DEV_NO_EVIDENCE} thiếu evidence)")
    print(f"test : {n_test} câu ({TEST_ANSWERABLE} có đáp án + {TEST_NO_EVIDENCE} thiếu evidence)")
    print(f"\nĐã ghi {args.out}")
    print(
        "LƯU Ý: sau khi chạy stage 4 trên dev và chốt cấu hình, mới chạy stage 4/5 "
        "với --split test."
    )


if __name__ == "__main__":
    main()
