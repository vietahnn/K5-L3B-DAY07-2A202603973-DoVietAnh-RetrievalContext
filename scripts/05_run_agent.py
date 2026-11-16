"""Stage 5 - run the agent over one split.

Usage:
    python scripts/05_run_agent.py --split dev --config hybrid
    python scripts/05_run_agent.py --split test --config rerank

This is the only stage that calls the language model, so it is the only
stage that costs money. The trace records, per question: every tool call and
its parameters, the final context, the answer, the citations, the number of
model calls, the elapsed time, the token usage and any error.

Writes:
    results/agent-<config>-<split>/traces.json    the raw run
    results/agent-<config>-<split>/cases.json     for the metrics script
    results/agent-<config>-<split>/reviews.template.json  a skeleton to fill by hand
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from _common import ROOT, banner, read_json, write_json

from src.agent import OpenAICompatibleController, RetrieverAdapter, run_question
from src.benchmark import load_questions, split_questions
from src.embeddings import build_embedder
from src.llm import LLMError, build_client
from src.models import Chunk
from src.retrievers import BM25Index, DenseRetriever, HybridRetriever, RerankRetriever
from src.store import ChromaStore


def build_retriever(name: str, args):
    """Build the retriever that the named configuration uses."""
    store = ChromaStore(args.index, args.dimensions)
    embedder = build_embedder()
    chunks = [Chunk.from_dict(c) for c in read_json(args.chunks)]
    lexical = BM25Index(
        [
            {
                "id": c.chunk_id,
                "text": c.text,
                "metadata": dict(c.metadata),
                "source_id": c.document_id,
                "source": c.source,
                "page": c.page,
            }
            for c in chunks
        ]
    )
    dense = DenseRetriever(store, embedder, strategy=args.strategy)
    if name == "dense":
        return dense
    hybrid = HybridRetriever(dense, lexical, candidates=args.candidates)
    if name == "hybrid":
        return hybrid
    if name == "rerank":
        return RerankRetriever(hybrid, candidates=args.candidates)
    raise SystemExit(f"config không hợp lệ: {name} (chọn dense, hybrid hoặc rerank)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chạy agent trên một split")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--config", choices=("dense", "hybrid", "rerank"), default="hybrid")
    parser.add_argument("--questions", type=Path, default=ROOT / "data" / "questions.json")
    parser.add_argument("--chunks", type=Path, default=ROOT / "data" / "chunks.json")
    parser.add_argument("--index", type=Path, default=ROOT / ".chroma")
    parser.add_argument("--strategy", default="structure")
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--candidates", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--max-searches", type=int, default=4)
    parser.add_argument("--budget", type=int, default=2800, help="ngân sách context theo ký tự")
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    args = parser.parse_args()

    banner(f"Stage 5 · Agent · config={args.config} · split={args.split}")

    all_questions = load_questions(args.questions)
    questions = split_questions(all_questions, args.split)
    if not questions:
        raise SystemExit(f"không có câu nào trong split '{args.split}'")
    print(f"câu hỏi: {len(questions)}")

    try:
        client, model = build_client()
    except LLMError as exc:
        raise SystemExit(f"không cấu hình được LLM: {exc}")
    print(f"model: {model}")

    retriever = build_retriever(args.config, args)

    traces: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for number, question in enumerate(questions, start=1):
        controller = OpenAICompatibleController(client, model)
        adapter = RetrieverAdapter(retriever)
        started = time.perf_counter()
        status = "answered"
        error = None
        result: dict[str, Any] = {}
        try:
            result = run_question(
                question["question"],
                controller,
                adapter,
                filters=question.get("filter"),
                max_searches=args.max_searches,
                top_k=args.top_k,
                budget=args.budget,
            )
            status = result.get("status", "answered")
        except Exception as exc:
            # A provider failure is recorded, never replaced by a fake answer.
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - started) * 1000

        events = result.get("events", [])
        search_events = [e for e in events if e.get("event") == "search"]
        context = search_events[-1].get("context", "") if search_events else ""
        context_ids = search_events[-1].get("context_ids", []) if search_events else []

        trace = {
            "query_id": question["id"],
            "split": question["split"],
            "question": question["question"],
            "answerable": question.get("answerable", True),
            "config": args.config,
            "status": status,
            "answer": result.get("answer", ""),
            "citations": result.get("citations", []),
            "events": events,
            "tool_calls": adapter.calls,
            "n_tool_calls": len(adapter.calls),
            "context": context,
            "context_ids": context_ids,
            "context_chars": len(context),
            "elapsed_ms": round(elapsed_ms, 1),
            "llm_usage": controller.usage_log,
            "llm_responses": controller.response_log,
            "error": error,
        }
        traces.append(trace)
        cases.append(
            {
                "id": question["id"],
                "split": question["split"],
                "question": question["question"],
                "answerable": question.get("answerable", True),
                "expected_behavior": (
                    "search_and_answer" if question.get("answerable", True)
                    else "search_and_abstain"
                ),
                "expected_answer": question.get("expected_answer"),
                "absence_reason": question.get("absence_reason"),
            }
        )
        note = error or f"{status}, {len(adapter.calls)} tool call(s)"
        print(f"  [{number}/{len(questions)}] {question['id']}: {note}")

    out_dir = args.results / f"agent-{args.config}-{args.split}"
    write_json(out_dir / "traces.json", traces)
    write_json(out_dir / "cases.json", cases)

    # A skeleton for the human labels. The agent's own output is never used as
    # the label: the grader reads the evidence and decides.
    reviews = []
    for trace in traces:
        review: dict[str, Any] = {
            "query_id": trace["query_id"],
            "citation_support": [False] * len(trace["citations"]),
        }
        if trace["answerable"]:
            review["answer_grade"] = None
            review["notes"] = ""
        else:
            review["abstention_correct"] = None
            review["notes"] = ""
        reviews.append(review)
    write_json(out_dir / "reviews.template.json", reviews)

    print(f"\nĐã ghi {out_dir}")
    print(
        "Bước tiếp theo: điền reviews.template.json bằng tay (đọc evidence để chấm), "
        "đổi tên thành reviews.json, rồi chạy stage 6."
    )


if __name__ == "__main__":
    main()
