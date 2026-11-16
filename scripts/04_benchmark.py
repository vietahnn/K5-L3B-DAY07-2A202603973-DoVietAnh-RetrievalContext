"""Stage 4 - benchmark the retrieval methods on one split.

Usage:
    python scripts/04_benchmark.py --method dense --split dev
    python scripts/04_benchmark.py --method bm25 --split dev
    python scripts/04_benchmark.py --method hybrid --split dev
    python scripts/04_benchmark.py --method rerank --split dev
    python scripts/04_benchmark.py --method all --split dev

Only answerable questions count towards Hit@1 and Evidence Hit@3. The
no-evidence questions are still run - to check that the retriever does not
surface confident-looking noise - but they are reported separately.

Writes:
    results/bench-<method>-<split>/traces.json
    results/bench-<method>-<split>/summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from _common import ROOT, banner, read_json, write_json

from src.benchmark import (
    load_questions,
    score_context,
    score_ranking,
    split_questions,
    summarize,
)
from src.embeddings import build_embedder
from src.models import Chunk
from src.retrievers import BM25Index, DenseRetriever, HybridRetriever, RerankRetriever
from src.store import ChromaStore

# The context the agent would actually receive, built exactly as the starter's
# ``pack_context`` builds it, so that the Context Evidence Rate measured here
# is the rate the agent really sees rather than a re-implementation of it.
STARTER = ROOT / "starter"
if str(STARTER) not in sys.path:
    sys.path.insert(0, str(STARTER))

from baseline import pack_context  # noqa: E402

#: Character budget for the context, matching the agent run.
CONTEXT_BUDGET = 2800

#: The methods under comparison. The order is the order they appear in the
#: report's comparison table.
METHODS = ("dense", "bm25", "hybrid", "rerank")


def build_retrievers(args, questions):
    """Build every retriever that the run needs."""
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
    hybrid = HybridRetriever(dense, lexical, candidates=args.candidates)
    rerank = RerankRetriever(hybrid, candidates=args.candidates)

    return {
        "dense": dense,
        "bm25": lexical,
        "hybrid": hybrid,
        "rerank": rerank,
    }


def run_method(name, retriever, questions, args) -> list[dict[str, Any]]:
    """Run one retrieval method over the questions and return the traces."""
    traces: list[dict[str, Any]] = []
    for question in questions:
        filters = question.get("filter")
        started = time.perf_counter()
        error = None
        results: list[dict[str, Any]] = []
        try:
            results = retriever.search(
                question["question"], filters=filters, top_k=args.top_k
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.perf_counter() - started) * 1000

        trace: dict[str, Any] = {
            "query_id": question["id"],
            "split": question["split"],
            "question": question["question"],
            "answerable": question.get("answerable", True),
            "method": name,
            "top_k": args.top_k,
            "filters_applied": filters,
            "results": [
                {
                    "id": r.get("id"),
                    "rank": r.get("rank"),
                    "score": r.get("score"),
                    "source_id": r.get("source_id"),
                    "source": r.get("source"),
                    "page": r.get("page"),
                    "text": r.get("text", ""),
                }
                for r in results
            ],
            "elapsed_ms": round(elapsed_ms, 1),
        }
        if error:
            trace["status"] = "error"
            trace["error"] = error
        else:
            trace["status"] = "ok"
            # The context is built the way the agent's context is built, so
            # that the Context Evidence Rate reported here is the rate the
            # agent really sees.
            context, context_ids = pack_context(results, budget=CONTEXT_BUDGET)
            trace["context"] = context
            trace["context_ids"] = context_ids
            trace["context_chars"] = len(context)
            # A no-evidence question has no gold, so it is scored only for
            # "did the retriever surface something", never for hit rate.
            if question.get("answerable", True):
                trace.update(score_ranking(results, question))
                trace["context_evidence"] = score_context(
                    context_ids, results, question
                )
            else:
                trace.update(
                    {
                        "hit_at_1": False,
                        "evidence_hit_at_3": False,
                        "first_hit_rank": None,
                        "reciprocal_rank": 0.0,
                        "context_evidence": False,
                    }
                )
        traces.append(trace)
    return traces


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark các phương pháp retrieval")
    parser.add_argument("--method", choices=(*METHODS, "all"), default="all")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--questions", type=Path, default=ROOT / "data" / "questions.json")
    parser.add_argument("--chunks", type=Path, default=ROOT / "data" / "chunks.json")
    parser.add_argument("--index", type=Path, default=ROOT / ".chroma")
    parser.add_argument("--strategy", default="structure", help="tên collection")
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--candidates", type=int, default=20, help="độ sâu candidate cho fusion")
    parser.add_argument("--results", type=Path, default=ROOT / "results")
    args = parser.parse_args()

    banner(f"Stage 4 · Benchmark retrieval · split={args.split}")

    all_questions = load_questions(args.questions)
    questions = split_questions(all_questions, args.split)
    if not questions:
        raise SystemExit(f"không có câu nào trong split '{args.split}'")
    n_answerable = sum(1 for q in questions if q.get("answerable", True))
    print(f"câu hỏi: {len(questions)} (có đáp án: {n_answerable}, thiếu evidence: {len(questions) - n_answerable})")

    methods = METHODS if args.method == "all" else (args.method,)
    retrievers = build_retrievers(args, questions)

    summaries = []
    for name in methods:
        print(f"\n--- {name} ---")
        traces = run_method(name, retrievers[name], questions, args)
        summary = summarize(traces, method=name, split=args.split)
        summaries.append(summary)
        out_dir = args.results / f"bench-{name}-{args.split}"
        write_json(out_dir / "traces.json", traces)
        write_json(out_dir / "summary.json", summary)
        print(
            f"Hit@1 {summary['hit_at_1']}/{summary['n_answerable']} "
            f"({summary['hit_at_1_pct']}%) · "
            f"Evidence Hit@3 {summary['evidence_hit_at_3']}/{summary['n_answerable']} "
            f"({summary['evidence_hit_at_3_pct']}%) · "
            f"Context Evidence Rate {summary['context_evidence_rate']}/{summary['n_answerable']} "
            f"({summary['context_evidence_rate_pct']}%) · "
            f"MRR {summary['mrr']} · "
            f"median {summary['median_ms']}ms · "
            f"context ~{summary['mean_context_chars']} chars"
        )

    # The combined summary accumulates across runs instead of overwriting, so
    # that running one method at a time still leaves a complete table.
    combined_path = args.results / f"bench-summary-{args.split}.json"
    combined: list[dict[str, Any]] = []
    if combined_path.exists():
        try:
            existing = json.loads(combined_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                combined = existing
        except json.JSONDecodeError:
            combined = []
    # Replace the entry of any method being re-run, keep the others.
    combined = [s for s in combined if s.get("method") not in methods]
    combined.extend(summaries)
    combined.sort(key=lambda s: (-s.get("evidence_hit_at_3_pct", 0), s["method"]))
    write_json(combined_path, combined)
    print(f"\nĐã ghi {combined_path}")
    print(f"\n{'method':<10}{'Hit@1':>9}{'EvHit@3':>11}{'CtxRate':>11}{'MRR':>8}{'med ms':>9}")
    for entry in combined:
        print(
            f"{entry['method']:<10}"
            f"{str(entry['hit_at_1']) + '/' + str(entry['n_answerable']):>9}"
            f"{str(entry['evidence_hit_at_3']) + '/' + str(entry['n_answerable']):>11}"
            f"{str(entry['context_evidence_rate']) + '/' + str(entry['n_answerable']):>11}"
            f"{entry['mrr']:<8}{entry['median_ms']:<9}"
        )


if __name__ == "__main__":
    main()
