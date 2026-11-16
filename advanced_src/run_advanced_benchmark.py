import json, os, time
from advanced_src.advanced_pipeline import AdvancedAgent

def run_benchmark(split_name="dev"):
    input_file = f"advanced_data/advanced_questions_{split_name}.json"
    with open(input_file, "r", encoding="utf-8") as f:
        questions = json.load(f)

    agent = AdvancedAgent(mode="auto")
    results = []
    group_stats = {}

    print(f"Running benchmark on {split_name} ({len(questions)} questions)...")

    for idx, q in enumerate(questions):
        res = agent.run_item(q)
        results.append(res)
        grp = q["group"]
        if grp not in group_stats:
            group_stats[grp] = {"total": 0, "correct": 0, "hits": 0}
        group_stats[grp]["total"] += 1
        if res["is_correct"]:
            group_stats[grp]["correct"] += 1
        if res["evidence_hit"]:
            group_stats[grp]["hits"] += 1

    total_correct = sum(1 for r in results if r["is_correct"])
    accuracy = total_correct / len(results) * 100

    summary = {
        "split": split_name,
        "total_questions": len(questions),
        "total_correct": total_correct,
        "accuracy_pct": round(accuracy, 2),
        "group_breakdown": group_stats,
        "results": results
    }

    output_file = f"advanced_results/benchmark_{split_name}_results.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[{split_name.upper()}] Done! Total: {total_correct}/{len(questions)} ({accuracy:.2f}%)")
    for g, s in group_stats.items():
        print(f"  - {g}: {s['correct']}/{s['total']} ({s['correct']/s['total']*100:.1f}%)")

    return summary

if __name__ == "__main__":
    run_benchmark("dev")
    run_benchmark("test")
