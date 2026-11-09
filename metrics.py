# -*- coding: utf-8 -*-
"""정답표도 판정 모델도 쓰지 않는 지표. 보는 것은 '만든 글'과 '실제로 읽은 원문 문단' 둘뿐이다.

kind
- signal : 높낮이를 보는 신호(조건끼리 비교)
- alarm  : 0이어야 하는 경보(0이 아니면 그 시행은 결과를 믿기 전에 원인부터 본다)
- evidence: 스위치가 실제 코드 경로를 바꿨는지 보는 증거(지표가 아니라 점검용)

실행: python -X utf8 metrics.py            # output/runs.jsonl 의 모든 시행 지표를 다시 계산해 표로 출력
"""
import itertools
import re
import sys
from collections import Counter

import common as C

METRICS = {
    # 이름: (kind, 어느 장치를 보는 신호인가, 한 줄 설명)
    "valid_citations":   ("signal", "서브에이전트 쓰기", "따옴표 문구가 그 에이전트가 실제로 읽은 문단에서 확인된 인용 수"),
    "valid_rate":        ("signal", "서브에이전트 쓰기", "인용 가운데 원문에서 확인된 비율"),
    "cited_docs":        ("signal", "배치(분업 폭)", "확인된 인용이 가리키는 서로 다른 문서 수 — 넓게 읽고 실제로 썼는가"),
    "read_use":          ("signal", "기획 배정·따라가기", "읽은 문서 가운데 인용까지 간 문서 비율 — 헛읽기가 적은가"),
    "core_read_ratio":   ("signal", "기획 배정", "읽은 글자 가운데 핵심 문서(relevance=core) 글자 비율"),
    "section_overlap":   ("signal", "구역 알림", "절끼리 읽은 문서 집합의 평균 자카드 겹침(낮을수록 구역이 갈림)"),
    "cite_density":      ("signal", "서브에이전트 쓰기", "본문 1,000자당 확인된 인용 수(문장 수 대신 글자 수로 센다)"),
    "budget_use":        ("signal", "대조군 공정성", "읽은 원문 글자 / 총 읽기 예산 — 두 시스템이 예산을 비슷하게 썼는가"),
    "gaps_left":         ("signal", "점검·재파견", "마지막 원고에도 부족 표시가 남은 절 수"),
    "planner_invalid_ids": ("signal", "기획", "기획이 내놓은 시작 자료 가운데 코퍼스에 없거나 다른 절과 겹쳐 코드가 걸러 낸 id 수"),
    "alarm_fabricated_quotes": ("alarm", "서브에이전트 쓰기", "읽은 문서를 가리키지만 따옴표 문구가 읽은 문단에 없는 인용 수"),
    "alarm_unread_citations":  ("alarm", "격리", "그 에이전트가 읽지 않은 문서를 가리키는 인용 수"),
    "alarm_malformed_citations": ("alarm", "서브에이전트 쓰기", "형식이 틀린 인용 수([d012] 처럼 문구가 없는 것)"),
    "alarm_draft_regression":  ("alarm", "두 번째 원고 처리", "재파견 뒤 최종 원고가 그 절의 앞 원고보다 확인 인용이 적은 절 수"),
    "alarm_source_leak":       ("alarm", "격리", "코디네이터에게 올라간 글(원고)에 원문 12단어 연속이 인용 밖에서 나온 횟수"),
    "alarm_over_budget":       ("alarm", "예산", "읽기 예산을 넘겨 읽은 글자 수"),
}
DROPPED = {
    "report_sentences": "문장 분리 규칙(마침표·줄바꿈)에 따라 값이 크게 바뀌어 한 줄로 설명되지 않아 버렸다. 글자 수만 쓴다.",
    "keyword_coverage": "질문 낱말이 보고서에 몇 번 나오나 — 한국어 보고서·영어 질문어라 번역어에 따라 흔들려 버렸다.",
}


def _section_reads(rec):
    """절 번호 → 그 절이 모든 바퀴에서 읽은 문단 id"""
    out = {}
    for a in rec.get("attempts", []):
        out.setdefault(a["sec"], []).extend(r["pid"] for r in a["read"])
    return out


def _strip_cites(md):
    return C.CITE.sub(" ", md)


def compute(rec):
    by_id = C.corpus()["by_id"]
    ix = C.index()
    m = {}
    if rec["system"] == "multi":
        sreads = _section_reads(rec)
        reads = [r for a in rec["attempts"] for r in a["read"]]
        finals = {int(k): rec["attempts"][v] for k, v in rec["chosen"].items()}
        cites = {"n": 0, "valid": 0, "fabricated": 0, "unread": 0, "malformed": 0, "valid_docs": set()}
        body_chars = 0
        for sec, a in finals.items():
            cr = C.citation_report(a["draft"], sreads.get(sec, []))
            for k in ("n", "valid", "fabricated", "unread", "malformed"):
                cites[k] += cr[k]
            cites["valid_docs"] |= set(cr["valid_docs"])
            body_chars += len(_strip_cites(a["draft"]))
        # 요약·맺음말에 인용이 섞였으면(규칙 위반) 읽지 않은 인용으로 센다 — 코디네이터는 원문을 읽지 않았다
        syn = " ".join(rec["synthesis"].get("summary", [])) + " " + rec["synthesis"].get("conclusion", "")
        g, loose = C.parse_citations(syn)
        cites["unread"] += len(g)
        cites["n"] += len(g)
        # 절끼리 겹침
        sets = [set(p.split("#")[0] for p in v) for v in sreads.values()]
        pairs = list(itertools.combinations(sets, 2))
        m["section_overlap"] = round(sum(len(a & b) / max(len(a | b), 1) for a, b in pairs) / len(pairs), 3) if pairs else None
        # 두 번째 원고 처리
        reg = 0
        for sec, a in finals.items():
            best = max(x["citations"]["valid"] for x in rec["attempts"] if x["sec"] == sec)
            if a["citations"]["valid"] < best:
                reg += 1
        m["alarm_draft_regression"] = reg
        sc = C.load_config()["sections"]
        m["gaps_left"] = sum(1 for a in finals.values()
                             if a["gap"] or a["citations"]["valid"] < sc["gap_min_valid_citations"]
                             or len(a["draft"]) < sc["gap_min_draft_chars"])
        pc = rec.get("plan_check", {})
        m["planner_invalid_ids"] = len(pc.get("invalid_ids", [])) + len(pc.get("dup_ids", []))
        up_text = "\n".join(_strip_cites(a["draft"]) for a in finals.values())
        coord = sum(x["prompt_chars"] for x in rec["llm_log"] if x["who"].startswith("coordinator"))
        res = [a["seen_chars"] for a in rec["attempts"]]
        m["coordinator_seen_chars"] = coord
        m["researchers_seen_chars_total"] = sum(res)
        m["researchers_seen_chars_max"] = max(res) if res else 0
        m["coordinator_source_chars"] = 0      # 코디네이터 프롬프트에는 원문 문단을 넣는 코드 경로가 없다(leak 경보로 확인)
        # 스위치 증거
        m["evidence_rounds"] = rec["rounds"]
        m["evidence_other_zone_prompt_share"] = round(sum(a["prompt_has_other_zones"] for a in rec["attempts"]) / max(len(rec["attempts"]), 1), 2)
        m["evidence_excluded_candidates"] = sum(a["excluded_other_zone_candidates"] for a in rec["attempts"])
        m["evidence_start_source"] = dict(Counter(s["start_source"] for s in rec["plan"]["sections"]))
        m["evidence_decisions"] = dict(Counter(d["decision"] for d in rec["decisions"]))
        m["n_sections"] = len(rec["plan"]["sections"])
    else:   # single (대조군)
        reads = rec["read"]
        pids = [r["pid"] for r in reads]
        cr = C.citation_report(rec["report_raw"], pids)
        cites = {k: cr[k] for k in ("n", "valid", "fabricated", "unread", "malformed")}
        cites["valid_docs"] = set(cr["valid_docs"])
        body_chars = len(_strip_cites(rec["report_raw"]))
        m["section_overlap"] = None
        m["alarm_draft_regression"] = 0
        m["gaps_left"] = None
        m["planner_invalid_ids"] = None
        up_text = ""                          # 위로 올라가는 단계가 없다
        m["coordinator_seen_chars"] = None
        m["researchers_seen_chars_total"] = sum(x["prompt_chars"] for x in rec["llm_log"])
        m["researchers_seen_chars_max"] = max((x["prompt_chars"] for x in rec["llm_log"]), default=0)
        m["n_sections"] = None

    read_chars = sum(r["chars"] for r in reads)
    docs_read = {r["doc"] for r in reads}
    core_chars = sum(r["chars"] for r in reads if by_id[r["doc"]].get("relevance") == "core")
    m["citations"] = cites["n"]
    m["valid_citations"] = cites["valid"]
    m["valid_rate"] = round(cites["valid"] / cites["n"], 3) if cites["n"] else 0.0
    m["cited_docs"] = len(cites["valid_docs"])
    m["docs_read"] = len(docs_read)
    m["read_use"] = round(len(cites["valid_docs"]) / len(docs_read), 3) if docs_read else 0.0
    m["read_chars"] = read_chars
    m["budget_use"] = round(read_chars / rec["budget_total"], 3)
    m["core_read_ratio"] = round(core_chars / read_chars, 3) if read_chars else 0.0
    m["report_chars"] = len(rec["report_raw"])
    m["body_chars"] = body_chars
    m["cite_density"] = round(cites["valid"] / body_chars * 1000, 2) if body_chars else 0.0
    m["alarm_fabricated_quotes"] = cites["fabricated"]
    m["alarm_unread_citations"] = cites["unread"]
    m["alarm_malformed_citations"] = cites["malformed"]
    m["alarm_over_budget"] = max(0, read_chars - rec["budget_total"])
    if up_text:
        src = set()
        for r in reads:
            src |= C.shingles(ix.by_pid[r["pid"]]["text"])
        m["alarm_source_leak"] = len(C.shingles(up_text) & src)
    else:
        m["alarm_source_leak"] = 0
    tok = Counter()
    for x in rec["llm_log"]:
        tok["in"] += x["in_tokens"]
        tok["out"] += x["out_tokens"]
    m["llm_calls"] = len(rec["llm_log"])
    m["in_tokens"] = tok["in"]
    m["out_tokens"] = tok["out"]
    return m


def main():
    runs = C.read_jsonl(C.OUT / "runs.jsonl")
    if not runs:
        print("output/runs.jsonl 이 비어 있다. 먼저 graph.py 나 baseline.py 를 돌린다.")
        return
    keys = ["valid_citations", "valid_rate", "cited_docs", "read_use", "core_read_ratio", "section_overlap",
            "budget_use", "gaps_left", "alarm_fabricated_quotes", "alarm_unread_citations", "alarm_draft_regression",
            "alarm_source_leak"]
    print("run_id".ljust(40), *[k[:10].rjust(10) for k in keys])
    for r in runs:
        m = compute(r)
        print(r["run_id"][:40].ljust(40), *[str(m.get(k)).rjust(10) for k in keys])


if __name__ == "__main__":
    main()
