# -*- coding: utf-8 -*-
"""스위치를 하나씩 끄고 같은 질문을 여러 번 돌려, 무엇이 값을 했는지 가른다 → output/ablation.json

조건: base(모두 켬) · 스위치 하나씩 끔(4개) · single(단일 에이전트 대조군)
한 번 돌린 결과로 단정하지 않는다: 같은 설정도 시행마다 크게 움직이므로 조건마다 --repeat 번 돌리고
평균과 함께 최소~최대를 적는다. 또 스위치가 실제 코드 경로를 바꿨는지 evidence_* 값으로 확인한다.

실행
  python -X utf8 ablation.py                       # 기본 질문 4개 × 6조건 × 3번
  python -X utf8 ablation.py --q q3 --repeat 2
  python -X utf8 ablation.py --aggregate-only      # 이미 있는 runs.jsonl 로 표만 다시 만든다
"""
import argparse
import json
import statistics
from concurrent.futures import ThreadPoolExecutor

import common as C
import graph
import baseline
from metrics import METRICS, compute

SWITCHES = ["use_planner_assignment", "tell_other_zones", "use_gap_check", "keep_better_draft"]
CONDITIONS = ["base"] + [f"no_{s}" for s in SWITCHES] + ["single"]
DEFAULT_Q = ["q2", "q3", "q5", "q6"]       # 한 건짜리 1 · 흩어진 것 2 · 따라가기 1
SHOW = ["valid_citations", "valid_rate", "cited_docs", "read_use", "core_read_ratio", "section_overlap",
        "cite_density", "budget_use", "gaps_left", "body_chars",
        "alarm_fabricated_quotes", "alarm_unread_citations", "alarm_malformed_citations",
        "alarm_draft_regression", "alarm_source_leak", "alarm_over_budget",
        "evidence_rounds", "evidence_other_zone_prompt_share", "evidence_excluded_candidates"]


def run_cond(cond, qid, r):
    if cond == "single":
        return baseline.run_one(qid, r)
    off = [] if cond == "base" else [cond[3:]]
    return graph.run_one(qid, off=off, tag=cond, repeat_idx=r)


def aggregate(qids=None):
    runs = C.read_jsonl(C.OUT / "runs.jsonl")
    if qids:
        runs = [r for r in runs if r["qid"] in qids]
    table = {}
    for cond in CONDITIONS:
        rs = [r for r in runs if r["tag"] == cond]
        if not rs:
            continue
        ms = [compute(r) for r in rs]
        row = {"n_runs": len(rs), "qids": sorted({r["qid"] for r in rs})}
        for k in SHOW:
            vals = [m[k] for m in ms if isinstance(m.get(k), (int, float))]
            if vals:
                row[k] = {"mean": round(statistics.mean(vals), 3), "min": min(vals), "max": max(vals),
                          "sd": round(statistics.pstdev(vals), 3)}
        per_q = {}
        for q in sorted({r["qid"] for r in rs}):
            qm = [compute(r) for r in rs if r["qid"] == q]
            per_q[q] = {k: round(statistics.mean([m[k] for m in qm if isinstance(m.get(k), (int, float))]), 3)
                        for k in ("valid_citations", "cited_docs", "core_read_ratio", "section_overlap", "budget_use", "gaps_left")
                        if any(isinstance(m.get(k), (int, float)) for m in qm)}
            per_q[q]["n"] = len(qm)
        row["per_question"] = per_q
        table[cond] = row
    out = {"conditions": table, "metric_doc": {k: {"kind": v[0], "device": v[1], "desc": v[2]} for k, v in METRICS.items()},
           "note": "평균(min~max, 표준편차). 같은 설정도 시행마다 움직이므로 차이가 min~max 범위 안이면 '차이 없음'으로 읽는다."}
    (C.OUT / "ablation.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def print_table(out):
    conds = list(out["conditions"])
    print("지표".ljust(34), *[c[:14].rjust(16) for c in conds])
    for k in SHOW:
        cells = []
        for c in conds:
            v = out["conditions"][c].get(k)
            cells.append((f"{v['mean']:.2f}({v['min']:g}~{v['max']:g})" if v else "-").rjust(16))
        print(k[:34].ljust(34), *cells)
    print("시행 수".ljust(34), *[str(out["conditions"][c]["n_runs"]).rjust(16) for c in conds])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", nargs="*", default=DEFAULT_Q)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--conds", nargs="*", default=CONDITIONS)
    ap.add_argument("--aggregate-only", action="store_true")
    args = ap.parse_args()
    if not args.aggregate_only:
        jobs = [(c, q, r) for r in range(args.repeat) for q in args.q for c in args.conds]
        print(f"시행 {len(jobs)}건 시작 (조건 {len(args.conds)} × 질문 {len(args.q)} × {args.repeat}번)", flush=True)
        def go(job):
            try:
                rec = run_cond(*job)
                m = rec["metrics"]
                print(f"ok {rec['run_id']}: 인용 {m['valid_citations']}/{m['citations']} 문서 {m['cited_docs']} "
                      f"예산 {m['budget_use']:.2f} 경보 {m['alarm_fabricated_quotes']}/{m['alarm_unread_citations']}", flush=True)
            except Exception as e:
                print(f"!! {job} 실패: {type(e).__name__}: {e}", flush=True)
        with ThreadPoolExecutor(C.load_config()["parallel"]["run_workers"]) as ex:
            list(ex.map(go, jobs))
    print_table(aggregate(args.q))


if __name__ == "__main__":
    main()
