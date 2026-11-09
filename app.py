# -*- coding: utf-8 -*-
"""웹 데모: 보고서와 함께, 절마다 누가 무엇을 읽고 무엇을 썼는지 보여 준다.

실행: streamlit run app.py      (→ http://localhost:8501)
- 저장된 시행(output/runs.jsonl)을 고르면 보고서·절별 분업·인용 대조·격리 숫자·지표를 본다.
- .env 에 API 키가 있으면 사이드바에서 질문을 골라 새로 돌릴 수 있다.
"""
import os
import re

import pandas as pd
import streamlit as st

import common as C
from metrics import METRICS, compute

st.set_page_config(page_title="딥리서처 데모", layout="wide")


@st.cache_data(show_spinner=False)
def load_runs(mtime):
    return C.read_jsonl(C.OUT / "runs.jsonl")


def runs():
    p = C.OUT / "runs.jsonl"
    return load_runs(p.stat().st_mtime if p.exists() else 0)


def title_of(d):
    return C.corpus()["by_id"][d]["title"]


def report_md(r):
    p = C.OUT / "reports" / f"{r['run_id']}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    from graph import render_footnotes
    return render_footnotes(r["report_raw"])


COND_NAME = {"base": "기본(모두 켬)", "no_use_planner_assignment": "기획 배정 끔", "no_tell_other_zones": "구역 알림 끔",
             "no_use_gap_check": "점검·재파견 끔", "no_keep_better_draft": "원고 비교 끔(덮어쓰기)",
             "single": "단일 에이전트 대조군"}

# ───────────────────────── 사이드바 ─────────────────────────
R = runs()
qs = C.load_questions()
qmap = {q["id"]: q for q in qs}
st.sidebar.title("딥리서처")
st.sidebar.caption(f"코퍼스: 위키백과 {C.corpus()['meta']['n_docs']}건 · "
                   f"{C.corpus()['meta']['total_chars']:,}자 · {C.corpus()['meta']['total_tokens_o200k']:,} 토큰 "
                   f"(창 {C.corpus()['meta']['context_window_tokens']:,}의 {C.corpus()['meta']['window_multiple']}배)")

has_key = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")) or C.FAKE
with st.sidebar.expander("새로 돌리기", expanded=not R):
    if not has_key:
        st.info(".env 에 API 키가 없어 새로 돌릴 수 없다. 저장된 시행만 볼 수 있다.")
    else:
        nq = st.selectbox("질문", [q["id"] for q in qs], format_func=lambda x: f"{x} · {qmap[x]['question'][:30]}…")
        cond = st.selectbox("조건", list(COND_NAME), format_func=COND_NAME.get)
        if st.button("실행", type="primary"):
            with st.spinner("기획 → 리서처 동시 파견 → 점검 → 종합 중… (1~3분)"):
                import ablation
                rec = ablation.run_cond(cond, nq, 0)
            st.session_state["pick"] = rec["run_id"]
            st.cache_data.clear()
            st.rerun()

if not R:
    st.title("딥리서처 데모")
    st.write("아직 저장된 시행이 없다. `python -X utf8 graph.py --q q3` 로 하나 돌리거나 사이드바에서 실행한다.")
    st.stop()

fq = st.sidebar.selectbox("질문으로 거르기", ["(전체)"] + sorted({r["qid"] for r in R}, key=lambda x: int(x[1:])))
fc = st.sidebar.selectbox("조건으로 거르기", ["(전체)"] + [c for c in COND_NAME if any(r["tag"] == c for r in R)],
                          format_func=lambda x: COND_NAME.get(x, x))
cand = [r for r in R if (fq == "(전체)" or r["qid"] == fq) and (fc == "(전체)" or r["tag"] == fc)]
ids = [r["run_id"] for r in cand][::-1]
default = ids.index(st.session_state["pick"]) if st.session_state.get("pick") in ids else 0
pick = st.sidebar.selectbox("시행", ids, index=default) if ids else None
if not pick:
    st.stop()
r = next(x for x in R if x["run_id"] == pick)
m = compute(r)

# ───────────────────────── 머리 ─────────────────────────
st.title(qmap[r["qid"]]["question"])
st.caption(f"{r['qid']} · 질문 성격: {qmap[r['qid']]['type']} · 왜 나눌 만한가: {qmap[r['qid']]['why_split']}")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("조건", COND_NAME.get(r["tag"], r["tag"]))
c2.metric("확인된 인용", f"{m['valid_citations']} / {m['citations']}")
c3.metric("인용한 문서", m["cited_docs"])
c4.metric("읽은 원문", f"{m['read_chars']:,}자", f"예산의 {m['budget_use']:.0%}", delta_color="off")
alarms = {k: m[k] for k in m if k.startswith("alarm_") and m[k]}
c5.metric("경보", "없음" if not alarms else f"{len(alarms)}종", delta_color="off")
if alarms:
    st.warning("0이어야 하는 경보: " + ", ".join(f"{k.replace('alarm_', '')}={v}" for k, v in alarms.items()))

tabs = st.tabs(["보고서", "절별 분업(누가 무엇을 읽고 썼나)", "인용 원문 대조", "격리·동시 파견", "지표", "조건 나란히 비교"])

# ───────────────────────── 보고서 ─────────────────────────
with tabs[0]:
    st.markdown(report_md(r))

# ───────────────────────── 절별 분업 ─────────────────────────
def attempts_by_sec(r):
    if r["system"] != "multi":
        return {}
    out = {}
    for i, a in enumerate(r["attempts"]):
        out.setdefault(a["sec"], []).append((i, a))
    return out

with tabs[1]:
    if r["system"] != "multi":
        st.info("단일 에이전트 대조군: 한 에이전트가 혼자 읽고 썼다.")
        st.write(f"읽은 문서 {len(r['docs_read'])}건, 원문 {m['read_chars']:,}자, 모델 호출 {m['llm_calls']}번")
        st.dataframe(pd.DataFrame([{"문서": d, "제목": title_of(d),
                                    "읽은 글자": sum(x["chars"] for x in r["read"] if x["doc"] == d)} for d in r["docs_read"]]),
                     hide_index=True, width="stretch")
    else:
        pc = r["plan_check"]
        st.caption(f"기획이 낸 절 {pc['proposed_sections']}개 · 코드가 걸러 낸 시작 자료: 없는 id {len(pc['invalid_ids'])}개 "
                   f"{pc['invalid_ids'] or ''}, 다른 절과 겹친 id {len(pc['dup_ids'])}개 · 검색으로 채운 절 {pc['auto_filled']}개")
        rows = []
        for sec in r["plan"]["sections"]:
            atts = [a for a in r["attempts"] if a["sec"] == sec["sec"]]
            fin = r["attempts"][r["chosen"][str(sec["sec"])]]
            rows.append({"절": sec["title"], "역할": sec["role"], "바퀴": len(atts),
                         "시작 자료": ", ".join(sec["start_docs"]),
                         "읽은 문서": sum(len(a["docs_read"]) for a in atts),
                         "읽은 원문(자)": sum(a["budget_used"] for a in atts),
                         "본 글자(자)": sum(a["seen_chars"] for a in atts),
                         "확인 인용": f"{fin['citations']['valid']}/{fin['citations']['n']}",
                         "부족 신고": "예" if any(a["gap"] for a in atts) else "",
                         "최종 원고": f"{fin['round']}바퀴째"})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        for d in r["decisions"]:
            if d["round"] > 1:
                st.caption(f"절 {d['sec']} 두 번째 원고: {d['decision']} (확인 인용 {d.get('old_valid')}→{d.get('new_valid')})")
        for sec in r["plan"]["sections"]:
            with st.expander(f"{sec['title']} — {sec['role']}"):
                st.write(f"**맡은 내용**: {sec['brief']}")
                st.write(f"**검색어**: {', '.join(sec['search_terms'])} · **시작 자료 출처**: {sec['start_source']}")
                for i, a in attempts_by_sec(r).get(sec["sec"], []):
                    st.markdown(f"**{a['round']}바퀴** · 예산 {a['budget']:,}자 중 {a['budget_used']:,}자 읽음 · "
                                f"본 글자 {a['seen_chars']:,}자 · 남의 구역 안내 {'있음' if a['prompt_has_other_zones'] else '없음'}")
                    docs = []
                    for d in a["docs_read"]:
                        docs.append({"문서": d, "제목": title_of(d), "핵심/주변": C.corpus()["by_id"][d].get("relevance", ""),
                                     "읽은 문단": sum(1 for x in a["read"] if x["doc"] == d),
                                     "읽은 글자": sum(x["chars"] for x in a["read"] if x["doc"] == d),
                                     "어떻게": "시작 자료" if d in a["start_docs"] else "따라가기"})
                    st.dataframe(pd.DataFrame(docs), hide_index=True, width="stretch")
                    for n in a["nav"]:
                        st.caption(f"따라가기: 후보 {n['n_candidates']}개 중 {', '.join(n['picked']) or '없음'} — {n['reason']}")
                    if a["gap"]:
                        st.caption(f"부족 신고: {a['missing']} · 더 찾을 말: {', '.join(a['more_terms'])}")
                    st.text_area("쓴 원고(인용 표시 그대로)", a["draft"], height=200, key=f"d{i}")

# ───────────────────────── 인용 대조 ─────────────────────────
with tabs[2]:
    st.caption("보고서의 인용 [d012 \"문구\"] 하나하나를, 그 절의 리서처가 실제로 읽은 문단에서 찾는다. 판정 모델을 쓰지 않는 글자 대조다.")
    ix = C.index()
    if r["system"] == "multi":
        units = [(sec["title"], r["attempts"][r["chosen"][str(sec["sec"])]]["draft"],
                  [x["pid"] for a in r["attempts"] if a["sec"] == sec["sec"] for x in a["read"]]) for sec in r["plan"]["sections"]]
    else:
        units = [("보고서 전체", r["report_raw"], [x["pid"] for x in r["read"]])]
    for name, md, pids in units:
        good, loose = C.parse_citations(md)
        rows = []
        for doc, quote, pos in good:
            hit = next((p for p in pids if p.startswith(doc + "#") and C.norm(quote) in C.norm(ix.by_pid[p]["text"])), None)
            read = any(p.startswith(doc + "#") for p in pids)
            verdict = C.check_quote(doc, quote, pids) if read else "unread"
            sent = md[max(0, md.rfind("\n", 0, pos)): pos].strip()[-120:]
            rows.append({"판정": {"exact": "✓ 원문 그대로", "elided": "△ 말줄임(단어는 원문에 있음)",
                                  "none": "✗ 원문에 없음", "unread": "✗ 읽지 않은 문서"}[verdict],
                         "문장(끝부분)": C.CITE.sub("", sent)[-80:], "문서": f"{doc} {title_of(doc)}",
                         "인용 문구": quote, "찾은 문단": hit or ""})
        st.subheader(name)
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if loose:
            st.caption(f"형식이 틀린 인용 {len(loose)}개")
    pid = st.text_input("문단 원문 보기 (예: d001#p03)")
    if pid and pid in ix.by_pid:
        st.code(ix.by_pid[pid]["text"], language=None)

# ───────────────────────── 격리·동시 파견 ─────────────────────────
with tabs[3]:
    if r["system"] != "multi":
        st.info("대조군은 한 컨텍스트에 모두 넣는다.")
        st.write(f"한 번에 본 최대 글자 수: {m['researchers_seen_chars_max']:,}자")
    else:
        a, b, c = st.columns(3)
        a.metric("코디네이터가 본 글자", f"{m['coordinator_seen_chars']:,}자", "문서 목록 + 원고만", delta_color="off")
        b.metric("리서처들이 본 글자(합)", f"{m['researchers_seen_chars_total']:,}자", f"한 리서처 최대 {m['researchers_seen_chars_max']:,}자", delta_color="off")
        c.metric("원문이 위로 샌 흔적", m["alarm_source_leak"], "20단어 연속 일치 수(0이어야)", delta_color="off")
        t0 = min(x["t_start"] for x in r["attempts"])
        tl = pd.DataFrame([{"절": f"{x['sec']}·{x['round']}바퀴", "시작(초)": round(x["t_start"] - t0, 1),
                            "끝(초)": round(x["t_end"] - t0, 1)} for x in r["attempts"]])
        st.write("리서처 실행 시각 — 같은 바퀴의 리서처들이 겹쳐 돌면 동시 파견이다")
        st.dataframe(tl, hide_index=True, width="stretch")
        st.write("모델 호출 기록")
        st.dataframe(pd.DataFrame(r["llm_log"]), hide_index=True, width="stretch")

# ───────────────────────── 지표 ─────────────────────────
with tabs[4]:
    rows = []
    for k, (kind, dev, desc) in METRICS.items():
        rows.append({"지표": k, "값": m.get(k), "종류": {"signal": "높낮이 신호", "alarm": "경보(0이어야)"}[kind],
                     "보는 장치": dev, "뜻": desc})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    ev = {k: v for k, v in m.items() if k.startswith("evidence_")}
    if ev:
        st.write("스위치가 실제 코드 경로를 바꿨는지 보는 증거")
        st.json(ev)

# ───────────────────────── 나란히 비교 ─────────────────────────
with tabs[5]:
    same = [x for x in R if x["qid"] == r["qid"]]
    conds = [c for c in COND_NAME if any(x["tag"] == c for x in same)]
    pickc = st.multiselect("비교할 조건", conds, default=[c for c in ("base", "single") if c in conds] or conds[:2],
                           format_func=COND_NAME.get)
    cols = st.columns(max(len(pickc), 1))
    for col, c in zip(cols, pickc):
        xs = [x for x in same if x["tag"] == c]
        x = xs[-1]
        mm = compute(x)
        col.markdown(f"**{COND_NAME[c]}** (시행 {len(xs)}번 중 마지막)")
        col.caption(f"확인 인용 {mm['valid_citations']}/{mm['citations']} · 문서 {mm['cited_docs']} · "
                    f"핵심 문서 비율 {mm['core_read_ratio']:.0%} · 본문 {mm['body_chars']:,}자")
        col.markdown(report_md(x))
