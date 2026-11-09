# -*- coding: utf-8 -*-
"""여러 파일이 같이 쓰는 부품: 설정·코퍼스·문단 검색·모델 호출·인용 검사·글자 수 세기.

핵심 약속
- "읽었다" = 원문 문단(passage)이 그 에이전트의 프롬프트에 들어갔다. 읽은 글자 수가 곧 예산 사용량이다.
- "본 글자 수(seen)" = 그 에이전트에게 들어간 프롬프트 전체 글자 수(지시문·목록·원문 포함). 격리를 잴 때 쓴다.
- 인용 형식은 [d012 "원문에서 그대로 옮긴 짧은 문구"] 하나로 통일한다. 따옴표 문구가 실제로 읽은 문단에
  있는지 코드로 대조할 수 있게 하려는 것이다(정답표·판정 모델 없이 근거를 검사하는 장치).
"""
import json
import math
import os
import re
import threading
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "output"
FAKE = os.environ.get("DR_FAKE") == "1"     # 키 없이 배관 시험(fake_llm.py). 이때는 output/_fake/ 에만 저장
if FAKE:
    OUT = ROOT / "output" / "_fake"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# ───────────────────────── 설정 ─────────────────────────
def load_config():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    m = cfg["model"]
    for role in ("planner", "researcher", "writer"):
        env = os.environ.get(f"DR_{role.upper()}_MODEL", "").strip()
        if env:
            m[role] = env
    return cfg


def load_questions():
    return json.loads((DATA / "questions.json").read_text(encoding="utf-8"))["questions"]


# ───────────────────────── 코퍼스와 문단 ─────────────────────────
HEADING = re.compile(r"^\s*(=+)\s*(.+?)\s*\1\s*$")


@lru_cache(maxsize=1)
def corpus():
    c = json.loads((DATA / "corpus.json").read_text(encoding="utf-8"))
    c["by_id"] = {d["id"]: d for d in c["docs"]}
    return c


def _split_passages(doc, target=900, max_len=1600):
    """문서를 900자 안팎 문단으로 자른다. 소제목은 문단 앞에 붙여 둔다."""
    out, buf, head = [], [], ""
    def flush():
        if buf:
            text = "\n".join(buf).strip()
            if text:
                out.append({"head": head, "text": text})
            buf.clear()
    for line in doc["text"].split("\n"):
        m = HEADING.match(line)
        if m:
            flush()
            head = m.group(2)
            continue
        if not line.strip():
            if sum(len(x) for x in buf) >= target:
                flush()
            continue
        buf.append(line)
        if sum(len(x) for x in buf) >= max_len:
            flush()
    flush()
    res = []
    for i, p in enumerate(out):
        res.append({"pid": f"{doc['id']}#p{i:02d}", "doc": doc["id"], "head": p["head"], "text": p["text"],
                    "chars": len(p["text"])})
    return res


TOKEN = re.compile(r"[a-z0-9]+")
STOP = set("the a an of and or to in on for by with is are was were be as at from that this which it its into "
           "their his her they he she not no can may also such other than these those has have had been".split())


def toks(s):
    return [t for t in TOKEN.findall(s.lower()) if t not in STOP and len(t) > 1]


class Index:
    """BM25 검색. 문단 단위(읽을 문단 고르기)와 문서 단위(첫 문단+제목, 찾아보기용) 둘을 둔다."""

    def __init__(self):
        c = corpus()
        self.passages = []
        for d in c["docs"]:
            self.passages += _split_passages(d)
        self.by_pid = {p["pid"]: p for p in self.passages}
        self.by_doc = {}
        for p in self.passages:
            self.by_doc.setdefault(p["doc"], []).append(p)
        self._p = self._build([(p["pid"], toks(p["head"] + " " + p["text"])) for p in self.passages])
        self._d = self._build([(d["id"], toks((d["title"] + " ") * 3 + d["text"][:1500])) for d in c["docs"]])

    @staticmethod
    def _build(items):
        df, docs = Counter(), []
        for key, t in items:
            tf = Counter(t)
            docs.append((key, tf, len(t)))
            df.update(tf.keys())
        n = len(docs)
        avg = sum(l for _, _, l in docs) / max(n, 1)
        idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        return {"docs": docs, "idf": idf, "avg": avg}

    @staticmethod
    def _score(ix, q, k1=1.4, b=0.75):
        qt = set(toks(q))
        res = []
        for key, tf, l in ix["docs"]:
            s = 0.0
            for w in qt:
                f = tf.get(w)
                if f:
                    s += ix["idf"][w] * f * (k1 + 1) / (f + k1 * (1 - b + b * l / ix["avg"]))
            if s > 0:
                res.append((s, key))
        res.sort(reverse=True)
        return res

    def search_docs(self, query, k=10, exclude=()):
        return [key for s, key in self._score(self._d, query) if key not in exclude][:k]

    def top_passages(self, doc_id, query, exclude=()):
        """한 문서 안에서 질의와 가까운 순서로 문단을 돌려준다(점수 0인 문단은 문서 순서대로 뒤에)."""
        ps = self.by_doc.get(doc_id, [])
        qt = set(toks(query))
        def sc(p):
            tf = Counter(toks(p["head"] + " " + p["text"]))
            return sum(self._p["idf"].get(w, 0) * min(tf.get(w, 0), 3) for w in qt)
        ranked = sorted((p for p in ps if p["pid"] not in exclude), key=lambda p: (-sc(p), p["pid"]))
        return ranked


_INDEX = None
_INDEX_LOCK = threading.Lock()


def index():
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = Index()
    return _INDEX


def doc_card(doc_id, n=160):
    d = corpus()["by_id"][doc_id]
    first = re.sub(r"\s+", " ", d["text"][:n * 2]).strip()[:n]
    return f"{doc_id} | {d['title']} | {first}…"


class Reader:
    """예산을 지키며 원문 문단을 읽는 도구. 읽은 문단은 모두 기록한다(나중에 인용 대조·격리 측정에 씀)."""

    def __init__(self, budget, per_doc_cap):
        self.budget = budget
        self.per_doc_cap = per_doc_cap
        self.used = 0
        self.read = []            # [{pid, doc, chars}]
        self._pids = set()

    @property
    def left(self):
        return self.budget - self.used

    def read_doc(self, doc_id, query, cap=None):
        """문서 하나에서 질의와 가까운 문단부터, 문서당 상한·남은 예산 안에서 읽는다."""
        ix = index()
        cap = min(cap or self.per_doc_cap, self.left)
        got, n = [], 0
        for p in ix.top_passages(doc_id, query, exclude=self._pids):
            if n + p["chars"] > cap:
                continue
            got.append(p)
            n += p["chars"]
            if cap - n < 300:
                break
        got.sort(key=lambda p: p["pid"])           # 읽기 좋게 문서 순서로
        for p in got:
            self._pids.add(p["pid"])
            self.read.append({"pid": p["pid"], "doc": p["doc"], "chars": p["chars"]})
        self.used += n
        return got

    def docs_read(self):
        seen, out = set(), []
        for r in self.read:
            if r["doc"] not in seen:
                seen.add(r["doc"])
                out.append(r["doc"])
        return out


def render_passages(ps):
    c = corpus()["by_id"]
    lines = []
    for p in ps:
        head = f" / {p['head']}" if p["head"] else ""
        lines.append(f"<<{p['doc']} | {c[p['doc']]['title']}{head}>>\n{p['text']}")
    return "\n\n".join(lines)


# ───────────────────────── 인용 ─────────────────────────
CITE = re.compile(r"\[(d\d{3})\s*[:,]?\s*[\"“]([^\n]{2,300}?)[\"”]\s*\]")   # 문구 안 큰따옴표·대괄호 허용
CITE_LOOSE = re.compile(r"\[(d\d{3})[^\]]*\]")


def norm(s):
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"[^a-z0-9가-힣']+", " ", s).strip()


def parse_citations(md):
    """보고서에서 인용을 뽑는다. 형식이 틀린 인용(따옴표 없음)도 따로 센다."""
    good = [(m.group(1), m.group(2), m.start()) for m in CITE.finditer(md)]
    spans = {m.start() for m in CITE.finditer(md)}
    loose = [(m.group(1), m.start()) for m in CITE_LOOSE.finditer(md) if m.start() not in spans]
    return good, loose


def _in_order(qw, tw):
    """문구 단어가 원문에 순서대로, 너무 흩어지지 않게(문구 길이의 2배+3 안) 들어 있는가 — 말줄임 판정."""
    if not qw:
        return False
    limit = 2 * len(qw) + 3
    for i, w in enumerate(tw):
        if w != qw[0]:
            continue
        k, j = 1, i + 1
        while k < len(qw) and j < len(tw) and j - i < limit:
            if tw[j] == qw[k]:
                k += 1
            j += 1
        if k == len(qw):
            return True
    return False


def check_quote(doc_id, quote, read_pids):
    """따옴표 문구를 그 에이전트가 실제로 읽은 같은 문서의 문단과 대조한다.
    exact  : 글자 그대로 있다
    elided : 단어는 순서대로 있지만 중간 낱말을 뺐다(말줄임) — 근거는 맞지만 형식 위반
    none   : 없다(지어낸 인용으로 본다)"""
    q = norm(quote)
    if len(q) < 4:
        return "none"
    ix = index()
    texts = [norm(ix.by_pid[pid]["text"]) for pid in read_pids if pid.startswith(doc_id + "#")]
    if any(q in t for t in texts):
        return "exact"
    qw = q.split()
    if any(_in_order(qw, t.split()) for t in texts):
        return "elided"
    return "none"


def citation_report(md, read_pids):
    good, loose = parse_citations(md)
    read_docs = {p.split("#")[0] for p in read_pids}
    valid, elided, fabricated, unread = [], [], [], []
    for doc, quote, pos in good:
        if doc not in read_docs:
            unread.append((doc, quote))
            continue
        v = check_quote(doc, quote, read_pids)
        (valid if v == "exact" else elided if v == "elided" else fabricated).append((doc, quote))
    return {"n": len(good), "valid": len(valid), "elided": len(elided), "fabricated": len(fabricated),
            "unread": len(unread), "malformed": len(loose), "valid_docs": sorted({d for d, _ in valid}),
            "elided_list": elided[:20], "fabricated_list": fabricated[:20], "unread_list": unread[:20]}


# ───────────────────────── 모델 호출 ─────────────────────────
_usage_lock = threading.Lock()


def make_llm(model, cfg):
    from langchain.chat_models import init_chat_model
    kw = {"timeout": 180, "max_retries": 3}
    m = cfg["model"]
    if m.get("temperature") is not None:
        kw["temperature"] = m["temperature"]
    if m.get("reasoning_effort") and (model.startswith("gpt") or model.startswith("openai:")):
        kw["reasoning_effort"] = m["reasoning_effort"]
    return init_chat_model(model, **kw)


def ask(model, cfg, system, user, schema=None, log=None, who=""):
    """모델을 한 번 부른다. 프롬프트 글자 수·토큰 사용량을 log(list)에 남긴다."""
    if FAKE:
        import fake_llm
        out = fake_llm.fake(schema, system, user)
        rec = {"who": who, "model": "FAKE", "prompt_chars": len(system) + len(user), "in_tokens": 0, "out_tokens": 0, "secs": 0}
        if log is not None:
            with _usage_lock:
                log.append(rec)
        return out, rec
    from langchain_core.messages import HumanMessage, SystemMessage
    llm = make_llm(model, cfg)
    msgs = [SystemMessage(system), HumanMessage(user)]
    t0 = time.time()
    if schema is not None:
        raw = llm.with_structured_output(schema, include_raw=True).invoke(msgs)
        out, msg = raw["parsed"], raw["raw"]
        if out is None:
            raise RuntimeError(f"{who}: 구조화 출력 파싱 실패 — {raw.get('parsing_error')}")
    else:
        msg = llm.invoke(msgs)
        out = msg.content if isinstance(msg.content, str) else "".join(
            x.get("text", "") for x in msg.content if isinstance(x, dict))
    u = getattr(msg, "usage_metadata", None) or {}
    rec = {"who": who, "model": model, "prompt_chars": len(system) + len(user),
           "in_tokens": u.get("input_tokens", 0), "out_tokens": u.get("output_tokens", 0),
           "secs": round(time.time() - t0, 1)}
    if log is not None:
        with _usage_lock:
            log.append(rec)
    return out, rec


def shingles(text, n=20):
    w = norm(text).split()
    return {" ".join(w[i:i + n]) for i in range(0, max(len(w) - n + 1, 0))}


def append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _usage_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
