"""Stage 7 - build the human review sheet and a draft of the labels.

BENCHMARK.md requires every test question to be read and labelled by a human,
with the reasoning recorded: the labels are what turn a run into a result.
Reading the raw traces is painful (each one is tens of kilobytes), so this
script pulls out exactly what a grader needs per question:

* the question, its expected answer and its gold evidence;
* the agent's answer, status and citations;
* the FULL text of every cited chunk, with its page;
* whether each cited chunk actually contains its gold quote (a computed fact,
  read with the same canonicalizer the retrieval metrics use).

It writes two files:

    review_sheet.md       what you read while grading
    reviews.draft.json    a proposed label for every question, with a note

The draft is a starting point, not a verdict. ``citation_support`` in the
draft means "this chunk contains a gold quote" - it does NOT mean "this
source supports every claim in the answer", which only reading can decide.
Edit the draft after reading the sheet, save it as ``reviews.json`` in the
same folder, then run stage 6.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from _common import ROOT, banner, read_json, write_json

from src.benchmark import load_questions, match_entry
from src.textutil import canon, evidence_present


def cited_chunk_texts(trace: dict[str, Any]) -> dict[str, str]:
    """Map every chunk id seen in this trace to its text."""
    texts: dict[str, str] = {}
    for call in trace.get("tool_calls", []):
        for result in call.get("results", []):
            texts[result["id"]] = result.get("text", "")
    return texts


def cite_contains_gold(
    chunk_text: str, citations_chunk: dict[str, Any], question: dict[str, Any]
) -> bool:
    """True when the chunk carries one of the question's gold entries."""
    return any(match_entry(citations_chunk, entry) for entry in question.get("gold", []))


def build_sheet(
    traces: list[dict[str, Any]], questions: list[dict[str, Any]]
) -> tuple[list[str], list[dict[str, Any]]]:
    """Render the markdown sheet and the draft labels."""
    by_id = {q["id"]: q for q in questions}
    lines: list[str] = [
        "# Phiếu chấm trace agent",
        "",
        "Chấm từng câu theo BENCHMARK.md:",
        "- Câu có đáp án: `answer_grade` 0 = sai/thiếu evidence, 1 = đúng một phần,",
        "  2 = đúng và đủ. abstain trên câu có đáp án = 0.",
        "- `citation_support`: một boolean cho MỖI citation, true khi nguồn đó",
        "  thực sự hỗ trợ câu trả lời (cờ `chứa gold` bên dưới là dữ kiện hỗ trợ,",
        "  không thay thế việc đọc).",
        "- Câu thiếu evidence: `abstention_correct` true khi agent từ chối và",
        "  không thêm khẳng định vô căn cứ.",
        "",
    ]
    reviews: list[dict[str, Any]] = []

    for trace in traces:
        question = by_id.get(trace["query_id"])
        if question is None:
            continue
        texts = cited_chunk_texts(trace)
        lines += [
            "---",
            "",
            f"## {trace['query_id']}  ·  {trace['status']}  ·  "
            f"{trace['n_tool_calls']} tool call(s)  ·  "
            f"{'có đáp án' if trace['answerable'] else 'THIẾU EVIDENCE'}",
            "",
            f"**Câu hỏi:** {question['question']}",
        ]
        if trace["answerable"]:
            lines.append(f"**Đáp án mong đợi:** {question.get('expected_answer', '')}")
            lines.append("**Gold evidence:**")
            for entry in question.get("gold", []):
                lines.append(f"- p{entry['page']}: “{entry['evidence']}”")
        else:
            lines.append(f"**Lý do thiếu evidence:** {question.get('absence_reason', '')}")

        lines += ["", f"**Agent trả lời:** {trace.get('answer') or '(trống)'}", ""]

        citations = trace.get("citations", [])
        support: list[bool] = []
        if citations:
            lines.append("**Citations:**")
            for cid in citations:
                chunk = {"id": cid, "text": texts.get(cid, "")}
                meta = next(
                    (
                        r.get("metadata", {})
                        for call in trace.get("tool_calls", [])
                        for r in call.get("results", [])
                        if r["id"] == cid
                    ),
                    {},
                )
                chunk = {"id": cid, "text": texts.get(cid, ""), "page": meta.get("page")}
                ok = cite_contains_gold(texts.get(cid, ""), chunk, question) if trace["answerable"] else False
                support.append(ok)
                page = meta.get("page", "?")
                flag = "✅ chứa gold" if ok else "⚠️ KHÔNG chứa gold"
                lines += [
                    f"- `{cid}` (trang {page}) — {flag if trace['answerable'] else 'câu không có gold'}",
                    f"  > {texts.get(cid, '(không thấy trong trace)')}",
                    "",
                ]
        else:
            support = []

        grade: Any = None
        note = ""
        if trace["answerable"]:
            if trace["status"] != "answered":
                grade = 0
                note = "abstain trên câu có đáp án -> 0 theo BENCHMARK.md. Đọc để xác nhận."
            elif support and all(support):
                grade = 2
                note = "CẢ NƯỚC: mọi citation chứa gold. ĐỌC câu trả lời và đối chiếu bản gốc trước khi chốt 2; hạ xuống 1 nếu thiếu điều kiện/số."
            elif any(support):
                grade = 1
                note = "MỘT SỐ citation chứa gold. Đọc để biết phần nào được hỗ trợ."
            else:
                grade = 0
                note = "không citation nào chứa gold quote. Đọc để xác nhận."
        else:
            grade = None
            correct = trace["status"] == "abstain"
            note = (
                "ĐỀ XUẤT abstention_correct=true (agent từ chối bằng câu cố định). "
                "Xác nhận agent không thêm khẳng định vô căn cứ."
                if correct
                else "Agent KHÔNG từ chối trên câu thiếu evidence -> abstention_correct=false, đọc kỹ."
            )

        review: dict[str, Any] = {
            "query_id": trace["query_id"],
            "citation_support": support,
            "notes": note,
        }
        if trace["answerable"]:
            review["answer_grade"] = grade
        else:
            review["abstention_correct"] = correct
        reviews.append(review)

    return lines, reviews


def main() -> None:
    parser = argparse.ArgumentParser(description="Dựng phiếu chấm + draft nhãn")
    parser.add_argument("--config", choices=("dense", "hybrid", "rerank"), default="hybrid")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--questions", type=Path, default=ROOT / "data" / "questions.json")
    args = parser.parse_args()

    banner(f"Stage 7 · Review sheet · config={args.config} · split={args.split}")

    run_dir = args.run_dir = ROOT / "results" / f"agent-{args.config}-{args.split}"
    traces = read_json(run_dir / "traces.json")
    questions = load_questions(args.questions)

    lines, reviews = build_sheet(traces, questions)
    sheet_path = run_dir / "review_sheet.md"
    sheet_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(run_dir / "reviews.draft.json", reviews)

    n_answerable = sum(1 for t in traces if t.get("answerable", True))
    print(f"câu có đáp án      : {n_answerable}")
    print(f"câu thiếu evidence : {len(traces) - n_answerable}")
    print(f"\nPhiếu chấm : {sheet_path}")
    print(f"Draft nhãn : {run_dir / 'reviews.draft.json'}")
    print(
        "\nBƯỚC CỦA BẠN: đọc review_sheet.md, sửa draft cho đúng thực tế,\n"
        "lưu thành reviews.json cùng thư mục, rồi chạy stage 6."
    )


if __name__ == "__main__":
    main()
