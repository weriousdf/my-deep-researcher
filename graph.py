# -*- coding: utf-8 -*-
"""딥리서처 파이프라인 (LangGraph): 기획 → 배치(동시 파견) → 리서처 → 점검(부족 신고 절만 재파견) → 종합.

실행 예
  python -X utf8 graph.py --q q3                      # 질문 하나, 기본 설정
  python -X utf8 graph.py --q q3 --off use_gap_check  # 스위치 하나 끄고
  python -X utf8 graph.py --all --repeat 2            # 질문 전부 두 번씩

격리 원칙: 원문 문단은 리서처 프롬프트에만 들어간다. 코디네이터(기획·점검·종합)에게는 문서 목록(제목+첫 줄)과
리서처가 쓴 원고만 올라간다. 둘의 글자 수를 따로 세어 runs.jsonl 에 남긴다.
"""
import argparse
import operator
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, List, TypedDict

from pydantic import BaseModel, Field

import common as C
from common import ask, corpus, doc_card, index, Reader, render_passages, citation_report


# ───────────────────────── 구조화 출력 모양 ─────────────────────────
class SectionPlan(BaseModel):
    title: str = Field(description="절 제목(한국어). 형식(배경/현황/전망)이 아니라 다루는 내용을 이름으로")
    role: str = Field(description="역할 명단의 key 하나")
    brief: str = Field(description="이 절이 답할 내용 2~3문장(한국어). 다른 절과 겹치지 않게")
    search_terms: List[str] = Field(description="코퍼스(영어)에서 찾을 영어 검색어 3~6개")
    start_docs: List[str] = Field(description="목록에 있는 문서 id(d000 꼴) 1~3개. 다른 절과 겹치지 않게")


class Plan(BaseModel):
    report_title: str = Field(description="보고서 제목(한국어)")
    sections: List[SectionPlan]


class NavChoice(BaseModel):
    doc_ids: List[str] = Field(description="다음에 읽을 문서 id. 후보 목록에 있는 것만")
    reason: str = Field(description="고른 이유 한 줄")


class Draft(BaseModel):
    draft: str = Field(description="이 절의 원고(한국어, 마크다운 문단). 사실마다 [d012 \"원문 문구\"] 인용")
    gap: bool = Field(description="읽은 자료만으로 이 절을 충분히 쓰지 못했으면 true")
    missing: str = Field(description="부족한 내용(없으면 빈 문자열)")
    more_terms: List[str] = Field(description="더 찾아볼 영어 검색어(없으면 빈 목록)")


class Synthesis(BaseModel):
    summary: List[str] = Field(description="보고서 첫머리 요약 3~5줄(한국어). 결론부터. 원고에 있는 내용만")
    conclusion: str = Field(description="맺음말 한 문단(한국어). 원고에 있는 내용만, 새 사실 금지")


# ───────────────────────── 프롬프트 ─────────────────────────
PLAN_SYS = """너는 리서치 코디네이터다. 원문은 읽지 않고, 문서 목록(id | 제목 | 첫 줄)만 보고 보고서 목차를 짠다.
규칙
1. 절은 형식(배경·현황·전망)이 아니라 내용 단위로 나눈다. 서로 다른 자료 묶음(제도·서류 종류·나라·절차 등)을 읽게 해야 한다.
   형식으로 나누면 모든 리서처가 같은 자료를 읽게 된다.
2. 절마다 역할 명단에서 역할 하나를 고르고, 다룰 내용·영어 검색어·시작 자료(목록에 있는 문서 id 1~{max_start}개)를 준다.
3. 두 절이 같은 시작 자료를 갖지 않게 한다.
4. 절은 최대 {nmax}개. 질문이 문서 한두 건으로 답이 나오면 절을 1~2개로 줄인다(억지로 나누지 않는다).
5. 목록에 없는 id를 지어내지 않는다."""

NAV_SYS = """너는 '{role}' 리서처다. 보고서 가운데 네가 맡은 절 하나만 책임진다.
지금 할 일: 후보 문서 가운데 네 절에 가장 도움이 될 문서를 최대 {k}개 고른다(없으면 빈 목록).
{zone_rule}"""

WRITE_SYS = """너는 '{role}' 리서처다. 맡은 절 하나의 원고를 한국어로 쓴다.
규칙
1. 아래 <원문> 문단에서 확인한 사실만 쓴다. 원문에 없는 것은 쓰지 않는다.
2. 사실을 쓴 문장 끝마다 근거를 [d012 "원문 문구"] 형식으로 붙인다. d012는 그 문단의 문서 id이고,
   따옴표 안은 그 문단에 있는 영어 문구를 3~15단어 글자 그대로 옮긴다(번역·요약·말줄임 금지). 인용 하나에 문서 하나.
3. 소제목 없이 3~6문단, 1,200~3,000자. 질문과 이 절의 내용(brief)에 답한다.
{zone_rule}
4. 다 쓴 뒤, 읽은 자료만으로 이 절을 충분히 쓰지 못했으면 gap=true, 부족한 내용(missing), 더 찾아볼 영어 검색어(more_terms)를 적는다."""

ZONE_ON = "다른 절이 맡은 구역(아래 <다른 절>)의 내용은 쓰지 않고, 그 절의 시작 자료는 고르지 않는다."
ZONE_OFF = ""

SYN_SYS = """너는 리서치 코디네이터다. 리서처들이 쓴 절 원고를 이어 붙여 보고서를 낸다. 원문은 볼 수 없고 원고만 본다.
할 일: 보고서 첫머리 요약(3~5줄, 결론부터)과 맺음말 한 문단을 한국어로 쓴다. 원고에 있는 내용만 쓰고 새 사실을 더하지 않는다.
요약·맺음말에는 인용 표시를 넣지 않는다(절 본문에 이미 있다)."""


# ───────────────────────── State ─────────────────────────
class State(TypedDict, total=False):
    run_id: str
    qid: str
    question: str
    switches: dict
    cfg: dict
    plan: dict
    plan_check: dict
    round: int
    pending: list          # 이번 바퀴에 보낼 절 번호
    budgets: dict          # 절 번호 → 이번 바퀴 읽기 예산
    attempts: Annotated[list, operator.add]
    chosen: dict           # 절 번호(str) → attempts 안의 위치
    decisions: Annotated[list, operator.add]
    llm_log: Annotated[list, operator.add]
    synthesis: dict
    report_raw: str


def _catalog():
    return "\n".join(doc_card(d["id"]) for d in corpus()["docs"])


# ───────────────────────── ① 기획 ─────────────────────────
def plan_node(s: State):
    cfg, sw = s["cfg"], s["switches"]
    sc = cfg["sections"]
    roles = cfg["roles"]
    roster = "\n".join(f"- {r['key']}: {r['name']} — {r['focus']}" for r in roles)
    sys = PLAN_SYS.format(max_start=sc["max_start_docs"], nmax=sc["n_sections_max"])
    user = f"<질문>\n{s['question']}\n\n<역할 명단>\n{roster}\n\n<문서 목록 {len(corpus()['docs'])}건>\n{_catalog()}"
    log = []
    plan, _ = ask(cfg["model"]["planner"], cfg, sys, user, schema=Plan, log=log, who="coordinator:plan")

    # 배정 검사: 실제 자료를 가리키는가, 겹치지 않는가 (코드가 검사한다)
    by_id = corpus()["by_id"]
    keys = {r["key"] for r in roles}
    check = {"invalid_ids": [], "dup_ids": [], "auto_filled": 0, "bad_roles": [], "dropped_sections": 0,
             "proposed_sections": len(plan.sections)}
    secs = plan.sections[: sc["n_sections_max"]]
    check["dropped_sections"] = len(plan.sections) - len(secs)
    taken, out = set(), []
    ix = index()
    for i, sp in enumerate(secs):
        role = sp.role if sp.role in keys else None
        if role is None:
            check["bad_roles"].append(sp.role)
            role = roles[i % len(roles)]["key"]
        start = []
        for d in sp.start_docs:
            d = d.strip()
            if d not in by_id:
                check["invalid_ids"].append(d)
            elif d in taken:
                check["dup_ids"].append(d)
            else:
                start.append(d)
        start = start[: sc["max_start_docs"]]
        source = "planner"
        if not sw["use_planner_assignment"]:
            start, source = [], "search(switch off)"
        if not start:
            q = " ".join(sp.search_terms) + " " + sp.brief
            excl = taken if sw["tell_other_zones"] else set()
            start = ix.search_docs(q, k=sc["max_start_docs"], exclude=excl)
            if sw["use_planner_assignment"]:
                check["auto_filled"] += 1
                source = "search(auto-fill)"
        taken.update(start)
        out.append({"sec": i, "title": sp.title, "role": role, "brief": sp.brief,
                    "search_terms": sp.search_terms, "start_docs": start, "start_source": source})
    n = len(out)
    per = int(sc["total_read_budget_chars"] * (1 - sc["reserve_ratio"]) / n)
    return {"plan": {"report_title": plan.report_title, "sections": out}, "plan_check": check,
            "round": 1, "pending": list(range(n)), "budgets": {str(i): per for i in range(n)},
            "chosen": {}, "llm_log": log}


# ───────────────────────── ② 배치 ─────────────────────────
def dispatch(s: State):
    from langgraph.types import Send
    secs = s["plan"]["sections"]
    sends = []
    for i in s["pending"]:
        prev = None
        if str(i) in s.get("chosen", {}):
            prev = s["attempts"][s["chosen"][str(i)]]
        prior_pids = [r["pid"] for a in s.get("attempts", []) if a["sec"] == i for r in a["read"]]
        others = [x for x in secs if x["sec"] != i]
        sends.append(Send("researcher", {
            "sec": secs[i], "others": others, "round": s["round"], "budget": s["budgets"][str(i)],
            "question": s["question"], "cfg": s["cfg"], "switches": s["switches"],
            "prev": prev, "prior_pids": prior_pids}))
    return sends


# ───────────────────────── 서브에이전트(리서처) ─────────────────────────
def researcher_node(p: dict):
    cfg, sw, sec = p["cfg"], p["switches"], p["sec"]
    sc = cfg["sections"]
    model = cfg["model"]["researcher"]
    role_name = next((r["name"] for r in cfg["roles"] if r["key"] == sec["role"]), sec["role"])
    t0 = time.time()
    log, seen = [], 0
    reader = Reader(p["budget"], sc["per_doc_cap_chars"])
    reader._pids.update(p["prior_pids"])          # 앞 바퀴에 읽은 문단은 다시 읽지 않는다
    prev = p["prev"]
    query = " ".join(sec["search_terms"]) + " " + p["question"]
    if prev:
        query = " ".join(prev.get("more_terms") or []) + " " + query

    zones_on = sw["tell_other_zones"]
    other_start = {d for o in p["others"] for d in o["start_docs"]} if zones_on else set()
    other_block = ""
    if zones_on:
        other_block = "\n<다른 절 — 이 구역은 다른 리서처가 맡는다>\n" + "\n".join(
            f"- {o['title']}: {o['brief']} (시작 자료: {', '.join(o['start_docs'])})" for o in p["others"])
    zone_rule = ZONE_ON if zones_on else ZONE_OFF
    excluded = 0

    # 1) 시작 자료 읽기 (2바퀴째는 부족 신고 검색어로 새 시작 자료를 찾는다)
    if prev:
        prior_docs = {x.split("#")[0] for x in p["prior_pids"]}
        start = index().search_docs(query, k=sc["max_start_docs"], exclude=prior_docs | other_start)
    else:
        start = list(sec["start_docs"])
    passages = []
    first_share = 0.6 * reader.budget
    for d in start:
        cap = min(sc["per_doc_cap_chars"], max(int(first_share / max(len(start), 1)), 1500))
        passages += reader.read_doc(d, query, cap=cap)

    # 2) 링크·검색으로 따라갈 문서 고르기 → 읽기 (예산이 남는 동안 최대 max_nav_steps 번)
    nav_reasons = []
    for step in range(sc.get("max_nav_steps", 3)):
        if reader.left < 1500:
            break
        read_docs = set(reader.docs_read()) | {x.split("#")[0] for x in p["prior_pids"]}
        cand = []
        for d in reader.docs_read():
            cand += corpus()["by_id"][d].get("out_links", [])
        cand += index().search_docs(query, k=12)
        uniq = []
        for d in cand:
            if d in read_docs or d in uniq:
                continue
            if d in other_start:
                excluded += 1
                continue
            uniq.append(d)
        uniq = uniq[: sc["max_candidates_shown"]]
        if not uniq:
            break
        nav_user = (f"<질문>\n{p['question']}\n\n<내 절>\n{sec['title']}: {sec['brief']}\n{other_block}\n\n"
                    f"<이미 읽은 문서>\n" + "\n".join(f"- {d} {corpus()['by_id'][d]['title']}" for d in reader.docs_read())
                    + "\n\n<후보 문서>\n" + "\n".join(doc_card(d) for d in uniq))
        nav_sys = NAV_SYS.format(role=role_name, k=sc["max_follow_docs"], zone_rule=zone_rule)
        choice, rec = ask(model, cfg, nav_sys, nav_user, schema=NavChoice, log=log, who=f"researcher{sec['sec']}:nav")
        seen += rec["prompt_chars"]
        picks = [d for d in choice.doc_ids if d in uniq][: sc["max_follow_docs"]]
        nav_reasons.append({"picked": picks, "reason": choice.reason, "n_candidates": len(uniq)})
        if not picks:
            break
        for d in picks:
            if reader.left < 800:
                break
            cap = min(sc["per_doc_cap_chars"], max(int(reader.left / len(picks)), 1500))
            passages += reader.read_doc(d, query, cap=cap)

    # 3) 원고 쓰기
    src = render_passages(passages)
    prev_block = ""
    if prev:
        prev_block = (f"\n\n<이전 원고 — 부족 신고: {prev.get('missing', '')}>\n{prev['draft']}\n"
                      "(이전 원고의 인용은 앞 바퀴에 읽은 원문에서 확인된 것이니 그대로 둘 수 있다. 부족한 내용을 새 원문으로 보강해 다시 쓴다.)")
    w_user = (f"<질문>\n{p['question']}\n\n<내 절>\n{sec['title']}: {sec['brief']}\n{other_block}{prev_block}\n\n"
              f"<원문 {len(passages)}개 문단>\n{src}")
    w_sys = WRITE_SYS.format(role=role_name, zone_rule=zone_rule)
    dr, rec = ask(model, cfg, w_sys, w_user, schema=Draft, log=log, who=f"researcher{sec['sec']}:write")
    seen += rec["prompt_chars"]

    all_pids = list(p["prior_pids"]) + [r["pid"] for r in reader.read]
    cites = citation_report(dr.draft, all_pids)
    attempt = {
        "sec": sec["sec"], "round": p["round"], "role": sec["role"], "title": sec["title"],
        "budget": reader.budget, "budget_used": reader.used, "read": reader.read,
        "docs_read": reader.docs_read(), "start_docs": start, "nav": nav_reasons,
        "seen_chars": seen, "source_chars_in_prompt": len(src),
        "prompt_has_other_zones": bool(other_block), "excluded_other_zone_candidates": excluded,
        "draft": dr.draft, "gap": dr.gap, "missing": dr.missing, "more_terms": dr.more_terms,
        "citations": cites, "t_start": t0, "t_end": time.time(),
    }
    return {"attempts": [attempt], "llm_log": log}


# ───────────────────────── ③ 점검 ─────────────────────────
def check_node(s: State):
    cfg, sw = s["cfg"], s["switches"]
    sc = cfg["sections"]
    chosen = dict(s.get("chosen", {}))
    decisions, flagged = [], []
    atts = s["attempts"]
    for i in s["pending"]:
        idx = max(k for k, a in enumerate(atts) if a["sec"] == i and a["round"] == s["round"])
        new = atts[idx]
        why = []
        if new["gap"]:
            why.append("리서처 부족 신고")
        if new["citations"]["valid"] < sc["gap_min_valid_citations"]:
            why.append(f"확인된 인용 {new['citations']['valid']}개 < {sc['gap_min_valid_citations']}")
        if len(new["draft"]) < sc["gap_min_draft_chars"]:
            why.append(f"원고 {len(new['draft'])}자 < {sc['gap_min_draft_chars']}")
        if str(i) not in chosen:
            chosen[str(i)] = idx
            decisions.append({"sec": i, "round": s["round"], "decision": "first", "flags": why})
        else:
            old = atts[chosen[str(i)]]
            if sw["keep_better_draft"]:
                ok = (new["citations"]["valid"] >= old["citations"]["valid"]
                      and len(new["draft"]) >= sc["keep_new_if_len_ratio"] * len(old["draft"]))
                dec = "replace" if ok else "keep_old"
            else:
                dec = "overwrite(switch off)"
            if dec != "keep_old":
                chosen[str(i)] = idx
            decisions.append({"sec": i, "round": s["round"], "decision": dec, "flags": why,
                              "old_valid": old["citations"]["valid"], "new_valid": new["citations"]["valid"]})
        if why:
            flagged.append(i)
    used = sum(a["budget_used"] for a in atts)
    left = sc["total_read_budget_chars"] - used
    nxt, budgets = [], {}
    if sw["use_gap_check"] and flagged and s["round"] < sc["max_rounds"] and left > 2000:
        per = int(min(sc["total_read_budget_chars"] * (1 - sc["reserve_ratio"]) / len(s["plan"]["sections"]),
                      left / len(flagged)))
        nxt = flagged
        budgets = {str(i): per for i in flagged}
    return {"chosen": chosen, "decisions": decisions, "pending": nxt, "budgets": budgets,
            "round": s["round"] + (1 if nxt else 0)}


def after_check(s: State):
    return dispatch(s) if s["pending"] else "synthesize"


# ───────────────────────── ④ 종합 ─────────────────────────
def synth_node(s: State):
    cfg = s["cfg"]
    secs = s["plan"]["sections"]
    parts = []
    for sec in secs:
        a = s["attempts"][s["chosen"][str(sec["sec"])]]
        parts.append(f"## {sec['title']}\n\n{a['draft'].strip()}")
    body = "\n\n".join(parts)
    user = f"<질문>\n{s['question']}\n\n<절 원고>\n{body}"
    log = []
    syn, _ = ask(cfg["model"]["writer"], cfg, SYN_SYS, user, schema=Synthesis, log=log, who="coordinator:synthesize")
    raw = (f"# {s['plan']['report_title']}\n\n> 질문: {s['question']}\n\n## 요약\n\n"
           + "\n".join(f"- {x}" for x in syn.summary) + "\n\n" + body + f"\n\n## 맺음말\n\n{syn.conclusion}\n")
    return {"synthesis": {"summary": syn.summary, "conclusion": syn.conclusion, "prompt_chars": len(SYN_SYS) + len(user)},
            "report_raw": raw, "llm_log": log}


def build_graph():
    from langgraph.graph import StateGraph, START, END
    g = StateGraph(State)
    g.add_node("plan", plan_node)
    g.add_node("researcher", researcher_node)
    g.add_node("check", check_node)
    g.add_node("synthesize", synth_node)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", dispatch, ["researcher"])
    g.add_edge("researcher", "check")
    g.add_conditional_edges("check", after_check, ["researcher", "synthesize"])
    g.add_edge("synthesize", END)
    return g.compile()


# ───────────────────────── 보고서 다듬기(인용 → 각주) ─────────────────────────
def render_footnotes(raw):
    by_id = corpus()["by_id"]
    notes, order = {}, []
    def sub(m):
        key = (m.group(1), C.norm(m.group(2)))
        if key not in notes:
            notes[key] = (len(notes) + 1, m.group(1), m.group(2))
            order.append(key)
        return f"[^{notes[key][0]}]"
    body = C.CITE.sub(sub, raw)
    foot = "\n".join(f"[^{n}]: {d} *{by_id[d]['title']}* — “{' '.join(q.split())}” ({by_id[d]['url']})"
                     for n, d, q in (notes[k] for k in order))
    return body + ("\n\n---\n\n" + foot + "\n" if foot else "")


# ───────────────────────── 실행 ─────────────────────────
def run_one(qid, off=(), tag=None, repeat_idx=0, save=True):
    import metrics
    cfg = C.load_config()
    q = next(x for x in C.load_questions() if x["id"] == qid)
    sw = dict(cfg["switches"])
    for k in off:
        if k not in sw:
            raise SystemExit(f"없는 스위치: {k} (있는 것: {list(sw)})")
        sw[k] = False
    tag = tag or ("base" if not off else "no_" + "+".join(off))
    run_id = f"{qid}-{tag}-{time.strftime('%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"
    t0 = time.time()
    g = build_graph()
    st = g.invoke({"run_id": run_id, "qid": qid, "question": q["question"], "switches": sw, "cfg": cfg,
                   "attempts": [], "decisions": [], "llm_log": []}, {"recursion_limit": 50})
    rec = {
        "run_id": run_id, "system": "multi", "tag": tag, "qid": qid, "qtype": q["type"], "question": q["question"],
        "repeat": repeat_idx, "switches": sw, "models": cfg["model"], "budget_total": cfg["sections"]["total_read_budget_chars"],
        "plan": st["plan"], "plan_check": st["plan_check"], "attempts": st["attempts"], "chosen": st["chosen"],
        "decisions": st["decisions"], "rounds": st["round"], "synthesis": {k: v for k, v in st["synthesis"].items()},
        "report_raw": st["report_raw"], "llm_log": st["llm_log"], "secs": round(time.time() - t0, 1),
    }
    rec["metrics"] = metrics.compute(rec)
    rec["synthesis"].pop("prompt_text_for_leak_check", None)
    if save:
        md = render_footnotes(st["report_raw"])
        (C.OUT / "reports").mkdir(parents=True, exist_ok=True)
        (C.OUT / "reports" / f"{run_id}.md").write_text(md, encoding="utf-8")
        C.append_jsonl(C.OUT / "runs.jsonl", rec)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", nargs="*", help="질문 id (예: q3 q5)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--off", nargs="*", default=[], help="끌 스위치 이름")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--tag")
    args = ap.parse_args()
    qids = [x["id"] for x in C.load_questions()] if args.all else (args.q or ["q3"])
    jobs = [(q, r) for r in range(args.repeat) for q in qids]
    cfg = C.load_config()
    def go(job):
        q, r = job
        try:
            rec = run_one(q, args.off, args.tag, r)
            m = rec["metrics"]
            print(f"{rec['run_id']}: 절 {len(rec['plan']['sections'])} · 바퀴 {rec['rounds']} · 인용 {m['valid_citations']}/{m['citations']} "
                  f"· 지어낸 인용 {m['alarm_fabricated_quotes']} · 읽음 {m['read_chars']:,}/{rec['budget_total']:,}자 · {rec['secs']}초", flush=True)
        except Exception as e:                       # 한 건 실패해도 나머지는 계속
            print(f"!! {q} r{r} 실패: {type(e).__name__}: {e}", flush=True)
    with ThreadPoolExecutor(cfg["parallel"]["run_workers"]) as ex:
        list(ex.map(go, jobs))


if __name__ == "__main__":
    main()
