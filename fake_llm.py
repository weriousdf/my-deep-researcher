# -*- coding: utf-8 -*-
"""API 키 없이 배관(그래프·지표·저장)을 시험하는 가짜 모델. 환경변수 DR_FAKE=1 일 때만 쓰인다.

실제 결과가 아니다. 가짜로 돌린 시행은 output/_fake/ 에만 저장되고 runs.jsonl 에는 들어가지 않는다.
"""
import random
import re

BLOCK = re.compile(r"<<(d\d{3}) \| [^>]*>>\n(.+?)(?=\n\n<<d\d{3} \||\Z)", re.S)
CARD = re.compile(r"^(d\d{3}) \|", re.M)


def _quote(text, rnd):
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*|\d+", text)
    if len(words) < 8:
        return None
    i = rnd.randrange(0, len(words) - 7)
    # 원문 그대로 이어진 단어여야 하므로 원문에서 다시 찾는다
    seg = " ".join(words[i:i + 6])
    m = re.search(r"\b" + r"\W+".join(map(re.escape, words[i:i + 6])) + r"\b", text)
    return m.group(0) if m else None


def fake(schema, system, user):
    rnd = random.Random(hash(user) & 0xFFFF)
    name = schema.__name__ if schema else "text"
    if name == "Plan":
        from graph import Plan, SectionPlan
        ids = CARD.findall(user)
        roles = re.findall(r"^- (\w+): ", user, re.M)
        secs = []
        for i, r in enumerate(roles[:4]):
            secs.append(SectionPlan(title=f"가짜 절 {i+1}", role=r, brief="가짜 brief",
                                    search_terms=["apostille", "notary", "translation"][i % 3:] + ["document"],
                                    start_docs=ids[i * 3:i * 3 + 2] + (["d999"] if i == 0 else [])))
        return Plan(report_title="가짜 보고서", sections=secs)
    if name in ("NavChoice",):
        from graph import NavChoice
        part = user.split("<후보 문서>")[-1]
        ids = CARD.findall(part)
        return NavChoice(doc_ids=ids[:3], reason="가짜")
    if name == "Terms":
        from baseline import Terms
        ids = CARD.findall(user)
        return Terms(search_terms=["apostille", "notary", "legalization"], start_docs=ids[:4])
    if name == "Draft":
        from graph import Draft
        paras = []
        for doc, text in BLOCK.findall(user)[:6]:
            q = _quote(text, rnd)
            if q:
                paras.append(f"가짜 문장입니다 [{doc} \"{q}\"].")
        paras.append('지어낸 인용 시험 [d001 "this phrase does not exist anywhere"].')
        return Draft(draft="\n\n".join(paras), gap=rnd.random() < 0.5, missing="가짜 부족", more_terms=["consular"])
    if name == "Synthesis":
        from graph import Synthesis
        return Synthesis(summary=["가짜 요약 1", "가짜 요약 2"], conclusion="가짜 맺음말")
    # 문자열 출력(대조군 보고서)
    paras = []
    for doc, text in BLOCK.findall(user)[:10]:
        q = _quote(text, rnd)
        if q:
            paras.append(f"가짜 문장 [{doc} \"{q}\"].")
    return "# 가짜 보고서\n\n## 요약\n\n- 가짜\n\n## 본문\n\n" + "\n\n".join(paras) + "\n\n## 맺음말\n\n가짜"
