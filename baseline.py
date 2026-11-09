# -*- coding: utf-8 -*-
"""단일 에이전트 대조군. 딥리서처와 같은 자료·같은 모델·같은 도구(검색·문단 읽기·링크 따라가기)·같은 총 읽기 예산.

다른 점은 하나뿐이다: 한 에이전트가 한 컨텍스트에서 계획하고, 읽고, 보고서 전체를 쓴다(분업·격리·점검 없음).
공정성 확인: runs.jsonl 의 budget_use(읽은 글자/예산)를 딥리서처와 나란히 본다. 대조군이 예산을 덜 쓰면
'대조군이 약해서 졌다'는 반론을 막을 수 없으므로, 예산이 남으면 검색 결과로 계속 읽게 했다.

실행: python -X utf8 baseline.py --q q3 [--repeat 2]   |   python -X utf8 baseline.py --all
"""
import argparse
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List

from pydantic import BaseModel, Field

import common as C
from common import ask, corpus, doc_card, index, Reader, render_passages
from graph import render_footnotes


class Terms(BaseModel):
    search_terms: List[str] = Field(description="코퍼스(영어)에서 찾을 영어 검색어 5~10개")
    start_docs: List[str] = Field(description="목록에 있는 문서 id 가운데 먼저 읽을 것 3~8개")


class NavChoice(BaseModel):
    doc_ids: List[str] = Field(description="다음에 읽을 문서 id. 후보 목록에 있는 것만")
    reason: str


START_SYS = """너는 리서처다. 혼자서 질문에 답하는 장문 보고서를 쓴다. 먼저 문서 목록(id | 제목 | 첫 줄)을 보고
영어 검색어와 먼저 읽을 문서를 고른다. 목록에 없는 id를 지어내지 않는다."""
NAV_SYS = "너는 리서처다. 후보 문서 가운데 보고서에 가장 도움이 될 문서를 최대 {k}개 고른다(없으면 빈 목록)."
WRITE_SYS = """너는 리서처다. 질문에 답하는 장문 보고서를 한국어 마크다운으로 쓴다.
규칙
1. 아래 <원문> 문단에서 확인한 사실만 쓴다.
2. 사실을 쓴 문장 끝마다 근거를 [d012 "원문 문구"] 형식으로 붙인다. d012는 그 문단의 문서 id이고,
   따옴표 안은 그 문단에 있는 영어 문구를 3~15단어 글자 그대로 옮긴다(번역·요약·말줄임 금지). 인용 하나에 문서 하나.
3. 구성: '# 제목' → '## 요약'(3~5줄 글머리, 결론부터, 인용 없이) → 내용별 '## 절' 2~5개(절마다 3~6문단) → '## 맺음말'.
   전체 길이는 6,000~12,000자."""


def run_one(qid, repeat_idx=0, save=True):
    import metrics
    cfg = C.load_config()
    sc = cfg["sections"]
    model = cfg["model"]["researcher"]          # 딥리서처의 리서처·기획·종합과 같은 모델
    q = next(x for x in C.load_questions() if x["id"] == qid)
    run_id = f"{qid}-single-{time.strftime('%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"
    t0 = time.time()
    log = []
    by_id = corpus()["by_id"]
    ix = index()
    budget = sc["total_read_budget_chars"]
    reader = Reader(budget, sc["per_doc_cap_chars"])

    catalog = "\n".join(doc_card(d["id"]) for d in corpus()["docs"])
    t, _ = ask(model, cfg, START_SYS, f"<질문>\n{q['question']}\n\n<문서 목록 {len(by_id)}건>\n{catalog}",
               schema=Terms, log=log, who="single:start")
    query = " ".join(t.search_terms) + " " + q["question"]
    invalid = [d for d in t.start_docs if d not in by_id]
    start = [d for d in t.start_docs if d in by_id][:8] or ix.search_docs(query, k=5)
    passages = []
    for d in start:
        if reader.left < 800:
            break
        passages += reader.read_doc(d, query, cap=min(sc["per_doc_cap_chars"], max(int(0.5 * budget / len(start)), 1500)))

    nav = []
    for step in range(8):                      # 딥리서처 리서처들의 따라가기 횟수 합(절 5 × 2)보다 넉넉하게
        if reader.left < 1500:
            break
        read = set(reader.docs_read())
        cand = []
        for d in reader.docs_read():
            cand += by_id[d].get("out_links", [])
        cand += ix.search_docs(query, k=15)
        uniq = []
        for d in cand:
            if d not in read and d not in uniq:
                uniq.append(d)
        uniq = uniq[: sc["max_candidates_shown"]]
        if not uniq:
            break
        user = (f"<질문>\n{q['question']}\n\n<이미 읽은 문서>\n" + "\n".join(f"- {d} {by_id[d]['title']}" for d in reader.docs_read())
                + "\n\n<후보 문서>\n" + "\n".join(doc_card(d) for d in uniq))
        ch, _ = ask(model, cfg, NAV_SYS.format(k=sc["max_follow_docs"]), user, schema=NavChoice, log=log, who=f"single:nav{step}")
        picks = [d for d in ch.doc_ids if d in uniq][: sc["max_follow_docs"]]
        if not picks:                           # 고를 게 없다고 하면 검색 순위대로 채워 예산을 쓴다(공정성)
            picks = uniq[:2]
        nav.append({"picked": picks, "reason": ch.reason, "n_candidates": len(uniq)})
        for d in picks:
            if reader.left < 800:
                break
            passages += reader.read_doc(d, query, cap=min(sc["per_doc_cap_chars"], max(int(reader.left / len(picks)), 1500)))

    passages.sort(key=lambda p: p["pid"])
    src = render_passages(passages)
    report, _ = ask(model, cfg, WRITE_SYS, f"<질문>\n{q['question']}\n\n<원문 {len(passages)}개 문단>\n{src}",
                    log=log, who="single:write")
    rec = {"run_id": run_id, "system": "single", "tag": "single", "qid": qid, "qtype": q["type"], "question": q["question"],
           "repeat": repeat_idx, "models": cfg["model"], "budget_total": budget, "read": reader.read,
           "docs_read": reader.docs_read(), "start_docs": start, "invalid_start_ids": invalid, "nav": nav,
           "report_raw": report, "llm_log": log, "secs": round(time.time() - t0, 1)}
    rec["metrics"] = metrics.compute(rec)
    if save:
        (C.OUT / "reports").mkdir(parents=True, exist_ok=True)
        (C.OUT / "reports" / f"{run_id}.md").write_text(render_footnotes(report), encoding="utf-8")
        C.append_jsonl(C.OUT / "runs.jsonl", rec)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    qids = [x["id"] for x in C.load_questions()] if args.all else (args.q or ["q3"])
    jobs = [(q, r) for r in range(args.repeat) for q in qids]
    def go(job):
        try:
            rec = run_one(*job)
            m = rec["metrics"]
            print(f"{rec['run_id']}: 인용 {m['valid_citations']}/{m['citations']} · 지어낸 인용 {m['alarm_fabricated_quotes']} "
                  f"· 읽음 {m['read_chars']:,}/{rec['budget_total']:,}자 · {rec['secs']}초", flush=True)
        except Exception as e:
            print(f"!! {job} 실패: {type(e).__name__}: {e}", flush=True)
    with ThreadPoolExecutor(C.load_config()["parallel"]["run_workers"]) as ex:
        list(ex.map(go, jobs))


if __name__ == "__main__":
    main()
