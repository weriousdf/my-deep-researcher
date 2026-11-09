# -*- coding: utf-8 -*-
"""위키백과에서 코퍼스를 모은다 → data/corpus.json (docs + links).

방법
1. 시드 문서(아포스티유·공증·영사확인·번역 인증·나라별 신분/가족/학력/범죄경력 서류)를 받는다.
2. 지금까지 모은 문서들이 가리키는 링크를 후보로 모으고, "모은 문서 몇 개가 가리키는가"로 순위를 매긴다.
3. 후보의 첫 문단을 받아 거른다.
   - 핵심 낱말(notary, apostille, legaliz, certif, translat, consul, registr ...)이 첫 문단에 있어야 한다.
   - "~는 나라/주/섬이다", "~는 회원국 N개의 연합이다", 인물 생몰년 같은 첫 문단은 뺀다.
     (1차 수집에서 Oregon·Cicero·Argentina 같은 문서가 섞여 들어와 이 규칙을 넣었다. REPORT.md 참고)
4. 총 토큰 수가 컨텍스트 윈도우 × 여유 배수를 넘을 때까지 본문을 받는다. 모자라면 2~3을 한 바퀴 더(최대 3단계).
5. 코퍼스 안 문서끼리의 링크만 남겨 links 로 저장하고, 들어오는 링크 0개 문서 수를 출력한다.

예의
- 요청은 한 번에 하나씩, 사이에 6초 쉰다. 429·403이 오면 그 자리에서 멈춘다(재시도하지 않음).
  받은 응답은 data/_cache 에 저장하므로, 시간을 두고 다시 돌리면 받은 데서부터 이어 받는다.
- User-Agent 는 일반 이름(이메일 없음).

실행:  python -X utf8 -I scripts/collect_wiki.py            (설정은 config.json 의 corpus·model 항목)
       python -X utf8 -I scripts/collect_wiki.py --dry       (본문은 새로 받지 않고 거르기 결과만 본다)
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "_cache"
API = "https://en.wikipedia.org/w/api.php"
UA = "my-deep-researcher-coursework/0.1 (educational project)"
SLEEP = 6.0   # 1.1초→16번째 요청에서 429, 3초→새 요청 10건쯤에서 429. 그래서 6초로 늘렸다.

SEEDS = [
    # 국제 문서 인증 제도
    "Apostille Convention", "Legalization (international law)", "Hague Conference on Private International Law",
    "Consular authentication", "Authentication (law)", "Certified copy",
    # 공증
    "Notary public", "Notary", "Civil law notary", "Notarial act", "Notaries in the United States",
    "Electronic notary", "Remote online notarization", "Jurat", "Acknowledgment (law)",
    # 번역 인증
    "Certified translation", "Sworn translator", "Legal translation", "Translation",
    # 진술서·위임
    "Affidavit", "Statutory declaration", "Power of attorney",
    # 나라별·종류별 서류
    "Birth certificate", "Marriage certificate", "Death certificate", "Vital record",
    "Family register", "Koseki", "Hukou", "Resident registration (South Korea)",
    "Police certificate", "Criminal record", "Diploma", "Transcript (education)",
    "Certificate of incorporation", "Diplomatic mission", "Consul (representative)",
    # 2차 추가(모델 창이 105만 토큰이라 코퍼스를 늘리면서, 업무와 가까운 문서로 채우려고 넣음)
    # 나라별 신분·여행 서류
    "Identity document", "Travel document", "Visa (document)", "Citizenship", "Permanent residency", "Work permit",
    "South Korean passport", "Japanese passport", "Chinese passport", "United States passport", "British passport",
    "Indian passport", "Philippine passport", "Vietnamese passport", "Resident registration number",
    "Individual Number", "Resident Identity Card", "Aadhaar", "Social Security number", "Real ID Act",
    "Driver's license", "Medical certificate",
    # 상업·법인 서류
    "Certificate of origin", "Commercial invoice", "Articles of incorporation", "Memorandum of association",
    # 관련 협약·전자 서명
    "Vienna Convention on Consular Relations", "Vienna Convention on Diplomatic Relations",
    "Hague Service Convention", "Hague Adoption Convention", "Hague Abduction Convention",
    "Electronic signature", "eIDAS",
    # 학력 인정
    "Credential evaluation", "Educational accreditation", "Lisbon Recognition Convention", "Bologna Process",
]

CORE_WORDS = re.compile(
    r"notar|apostil|legali[sz]|authenticat|attest|certif|translat|consul|embass|affidavit|"
    r"oath|registr|civil status|vital record|identity document|travel document|legal document|"
    r"official document|public document|instrument|deed|seal|signature|testimon|evidence|"
    r"passport|visa|diploma|academic degree|transcript|power of attorney|witness|diplomatic mission",
    re.I)
NOT_TOPIC = re.compile(
    r"\bis (?:an?|the) (?:[\w'-]+ ){0,4}(?:country|state in|state of|island|crown dependency|"
    r"city|province|territory|region of|political and economic union|association of \d+ member states|"
    r"intergovernmental organi[sz]ation of states)\b"
    r"|\b\d{1,4} (?:BC|AD) [–-] "                 # 인물 생몰년 (106 BC – 43 BC)
    r"|\bwas an? (?:[\w'-]+ ){0,3}(?:statesman|philosopher|politician|king|emperor|poet)\b",
    re.I)
# 모은 뒤 "핵심/주변" 표시용(거르기와 별개). 앞 3000자에 이 낱말이 2종류 이상이면 핵심, 시드는 모두 핵심.
CORE_DOC = re.compile(r"notar|apostil|legali[sz]|authenticat|attest|certif|affidavit|oath|registr|civil status|vital record|"
                      r"identity document|passport|visa|diploma|transcript|power of attorney|consul|deed|seal|signature|sworn", re.I)
SKIP_TITLE = re.compile(r"^(List of|Index of|Outline of|Category:|Template:|Wikipedia:|Help:|Portal:|File:)|\(disambiguation\)|^\d", re.I)
TAIL_SECTIONS = re.compile(r"\n==\s*(See also|References|Notes|External links|Further reading|Bibliography|Sources|Citations|Footnotes)\s*==.*", re.S)


class Stop(Exception):
    pass


_last = [0.0]
_calls = [0]


def call(params):
    """위키백과 API 한 번 부르기. 순서대로, 천천히."""
    wait = SLEEP - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    params = {**params, "format": "json", "formatversion": "2"}
    r = requests.get(API, params=params, headers={"User-Agent": UA}, timeout=30)
    _last[0] = time.time()
    _calls[0] += 1
    if r.status_code in (403, 429):
        raise Stop(f"HTTP {r.status_code} — 위키백과가 요청을 막아 멈춘다")
    r.raise_for_status()
    return r.json()


def _cache_path(name):
    safe = re.sub(r"[^\w\-]+", "_", name)[:120]
    return CACHE / f"{safe}.json"


def cached(name, fn):
    CACHE.mkdir(parents=True, exist_ok=True)
    p = _cache_path(name)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    data = fn()
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def fetch_page(title, offline=False):
    """본문(평문)과 나가는 링크(본문 이름공간)를 받는다. 넘겨주기(redirect)는 따라간다."""
    if offline and not _cache_path("page_" + title).exists():
        return None

    def _f():
        text, links, cont = None, [], {}
        while True:
            d = call({"action": "query", "titles": title, "redirects": 1,
                      "prop": "extracts|links|info", "explaintext": 1, "exsectionformat": "wiki",
                      "plnamespace": 0, "pllimit": "max", "inprop": "url", **cont})
            pages = d.get("query", {}).get("pages", [])
            if not pages or pages[0].get("missing"):
                return None
            p = pages[0]
            if text is None:
                text, url, pid, final = p.get("extract", ""), p.get("fullurl", ""), p.get("pageid"), p["title"]
            links += [l["title"] for l in p.get("links", [])]
            if "continue" in d and "plcontinue" in d["continue"]:
                cont = {"plcontinue": d["continue"]["plcontinue"], "continue": d["continue"]["continue"]}
            else:
                break
        return {"title": final, "pageid": pid, "url": url, "text": text, "out": links}
    return cached("page_" + title, _f)


def load_intro_cache():
    """예전 실행에서 받은 첫 문단을 모두 읽어 둔다(같은 후보를 다시 부르지 않기 위해)."""
    got = {}
    if CACHE.exists():
        for p in CACHE.glob("intro_*.json"):
            got.update(json.loads(p.read_text(encoding="utf-8")))
    return got


def fetch_intros(titles, known, offline=False):
    """아직 모르는 후보만 20개씩 첫 문단을 받는다."""
    todo = [t for t in titles if t not in known]
    if offline:
        return known
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]

        def _f(chunk=chunk):
            d = call({"action": "query", "titles": "|".join(chunk), "redirects": 1, "prop": "extracts",
                      "exintro": 1, "explaintext": 1, "exlimit": 20})
            q = d.get("query", {})
            norm = {n["to"]: n["from"] for n in q.get("normalized", [])}
            back = {r["to"]: norm.get(r["from"], r["from"]) for r in q.get("redirects", [])}
            res = {t: None for t in chunk}
            for p in q.get("pages", []):
                if p.get("missing"):
                    continue
                orig = back.get(p["title"], norm.get(p["title"], p["title"]))
                res[orig] = {"title": p["title"], "intro": p.get("extract", "")}
            return res
        known.update(cached("intro_" + "_".join(c[:12] for c in chunk[:3]) + f"_{len(chunk)}_{abs(hash(tuple(chunk))) % 10**8}", _f))
    return known


def clean(text):
    text = TAIL_SECTIONS.sub("", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def judge(intro):
    """첫 문단으로 주제 문서인지 가린다. (통과 여부, 핵심 낱말 수, 이유)"""
    head = (intro or "")[:800]
    if NOT_TOPIC.search(head[:400]):
        return False, 0, "나라·지역·인물 문서"
    hits = len(set(m.group(0).lower() for m in CORE_WORDS.finditer(head)))
    if hits < 1:
        return False, 0, "핵심 낱말 없음"
    return True, hits, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="본문을 새로 받지 않는다(캐시만 사용)")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ccfg = cfg["corpus"]
    window = cfg["model"]["context_window_tokens"]
    target_tokens = int(window * ccfg["target_window_multiple"])
    max_docs, min_chars = ccfg["max_docs"], ccfg["min_chars"]

    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")

    docs, seen, rejected = [], set(), {}
    alias = {}                       # 링크에 적힌 이름 → 실제 문서 제목(넘겨주기)
    total_tok = 0
    intros = load_intro_cache()

    def add(p, source):
        nonlocal total_tok
        p["text"] = clean(p["text"])
        if len(p["text"]) < min_chars:
            rejected[p["title"]] = f"너무 짧음({len(p['text'])}자)"
            return False
        p["source"] = source
        seen.add(p["title"])
        docs.append(p)
        total_tok += len(enc.encode(p["text"]))
        print(f"{source:7s}{len(docs):4d} {p['title'][:48]:48s} {len(p['text']):7d}자  누적 {total_tok:,} tok")
        return True

    try:
        # 1) 시드
        for t in SEEDS:
            p = fetch_page(t, offline=args.dry)
            if not p:
                print(f"  - 시드 없음: {t}", file=sys.stderr)
                continue
            alias[t] = p["title"]
            if p["title"] in seen:
                print(f"  - 시드 중복(넘겨주기): {t} → {p['title']}", file=sys.stderr)
                continue
            add(p, "seed")

        # 2~4) 단계별 확장
        for hop in (1, 2, 3):
            if total_tok >= target_tokens or len(docs) >= max_docs:
                break
            votes = {}
            for d in docs:
                for l in set(d["out"]):
                    if alias.get(l, l) in seen or l in rejected or SKIP_TITLE.search(l):
                        continue
                    votes[l] = votes.get(l, 0) + 1
            cands = sorted((c for c in votes if votes[c] >= 2), key=lambda x: (-votes[x], x))[:ccfg["candidate_pool"]]
            print(f"\n[{hop}단계] 후보 {len(cands)}개 (모은 문서 2개 이상이 가리킴). 첫 문단으로 거르기…")
            intros = fetch_intros(cands, intros, offline=args.dry)
            scored = []
            for c in cands:
                it = intros.get(c)
                if not it:
                    continue
                alias[c] = it["title"]
                if it["title"] in seen:
                    continue
                ok, hits, why = judge(it["intro"])
                if not ok:
                    rejected[c] = why
                    continue
                scored.append((votes[c] * 2 + hits, c))
            scored.sort(key=lambda x: -x[0])
            print(f"[{hop}단계] 통과 {len(scored)}개")
            for s, c in scored:
                if total_tok >= target_tokens or len(docs) >= max_docs:
                    break
                p = fetch_page(c, offline=args.dry)
                if not p or p["title"] in seen:
                    continue
                add(p, f"hop{hop}")
    except Stop as e:
        print(f"\n!! {e}. 지금까지 받은 {len(docs)}건으로 저장한다. 시간을 두고 다시 돌리면 이어 받는다.", file=sys.stderr)

    # 5) 코퍼스 안 링크만 남긴다
    title2id = {}
    for i, d in enumerate(docs, 1):
        d["id"] = f"d{i:03d}"
        title2id[d["title"]] = d["id"]
    links = set()
    for d in docs:
        for l in d["out"]:
            tgt = title2id.get(alias.get(l, l))
            if tgt and tgt != d["id"]:
                links.add((d["id"], tgt))
    links = sorted(links)
    indeg = {d["id"]: 0 for d in docs}
    outdeg = {d["id"]: 0 for d in docs}
    for a, b in links:
        outdeg[a] += 1
        indeg[b] += 1

    out_docs = []
    for d in docs:
        core_hits = len(set(m.group(0).lower() for m in CORE_DOC.finditer(d["text"][:3000])))
        out_docs.append({"id": d["id"], "title": d["title"], "url": d["url"], "source": d["source"],
                         "relevance": "core" if (d["source"] == "seed" or core_hits >= 2) else "peripheral",
                         "chars": len(d["text"]), "tokens": len(enc.encode(d["text"])),
                         "out_links": sorted({b for a, b in links if a == d["id"]}),
                         "text": d["text"]})
    total_chars = sum(x["chars"] for x in out_docs)
    total_tok = sum(x["tokens"] for x in out_docs)
    zero_in = [d["id"] for d in out_docs if indeg[d["id"]] == 0]
    zero_any = [d["id"] for d in out_docs if indeg[d["id"]] == 0 and outdeg[d["id"]] == 0]
    reasons = {}
    for why in rejected.values():
        k = why.split("(")[0]
        reasons[k] = reasons.get(k, 0) + 1
    meta = {
        "source": "English Wikipedia (CC BY-SA 4.0), MediaWiki API, plain-text extracts",
        "collected_at": time.strftime("%Y-%m-%d"),
        "n_docs": len(out_docs), "n_links": len(links),
        "total_chars": total_chars, "total_tokens_o200k": total_tok,
        "context_window_tokens": window,
        "exceeds_window": total_tok > window,
        "window_multiple": round(total_tok / window, 2),
        "zero_inlink_docs": zero_in, "zero_link_docs": zero_any,
        "seeds": [d["title"] for d in docs if d["source"] == "seed"],
        "by_source": {s: sum(1 for d in out_docs if d["source"] == s) for s in sorted({d["source"] for d in out_docs})},
        "rejected_candidates": reasons,
        "core": {"docs": sum(1 for d in out_docs if d["relevance"] == "core"),
                 "chars": sum(d["chars"] for d in out_docs if d["relevance"] == "core"),
                 "tokens": sum(d["tokens"] for d in out_docs if d["relevance"] == "core")},
    }
    corpus = {"meta": meta, "docs": out_docs, "links": [{"src": a, "dst": b} for a, b in links]}
    if not args.dry:
        (ROOT / "data" / "corpus.json").write_text(json.dumps(corpus, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n===== 코퍼스 요약 =====" + (" (--dry: 저장 안 함)" if args.dry else ""))
    print(f"문서 {len(out_docs)}건 {meta['by_source']}  · 이번 실행 API 요청 {_calls[0]}회")
    print(f"걸러 낸 후보 {sum(reasons.values())}개 {reasons}")
    print(f"핵심 문서 {meta['core']['docs']}건 {meta['core']['chars']:,}자 {meta['core']['tokens']:,} tok / 나머지는 주변 문서")
    print(f"링크 {len(links)}개 (코퍼스 안 문서끼리)")
    print(f"총 글자 수 {total_chars:,}자 / 총 토큰 {total_tok:,} (o200k_base)")
    print(f"컨텍스트 윈도우 {window:,} 토큰 → {meta['window_multiple']}배, 넘는가: {meta['exceeds_window']}")
    print(f"들어오는 링크 0개 문서 {len(zero_in)}건(배정이 유일한 경로): {zero_in}")
    print(f"들어오고 나가는 링크 모두 0개 문서 {len(zero_any)}건: {zero_any}")


if __name__ == "__main__":
    main()
