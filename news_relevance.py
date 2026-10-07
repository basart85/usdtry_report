"""
Rank news headlines by how well they match an "up day" or "down day" description of USD/TRY.

Two interchangeable scoring back-ends:

  tfidf  (default, no model, no extra packages; what runs in CI / on the deployed site)
         A finance-aware tokenizer -> TF-IDF vectors -> cosine similarity with the prompt.
         Tokenizer steps:
           1. Unicode fold + lowercase (Erdoğan -> erdogan, Türkiye -> turkiye)
           2. Phrase merges  ("federal reserve" -> fed, "central bank" -> centralbank,
              "strait of hormuz" -> hormuz, "u.s." -> us, ...)
           3. Word tokens, stop-word removal, light stemming (rates->rate, hiked->hike)
           4. Concept mapping: synonyms share one token (raise/tighten -> hike,
              crude/brent/fuel -> energy, payrolls/employment -> jobs, ...)
           5. Context tokens per sentence: a policy word next to a country becomes
              @us_hike / @tr_hike / @us_cut / @tr_cut, because a Fed hike and a Turkish
              central-bank hike push USD/TRY in opposite directions
           6. Unigrams + adjacent bigrams

  ollama (optional, local) Embeddings from a local Ollama server (default model
         nomic-embed-text) and cosine similarity. Used with --sim-backend ollama, or
         automatically with --sim-backend auto when the server answers.

Usage (standalone):
    python news_relevance.py news.json --direction up --top 5
    python news_relevance.py news.json --direction down --backend ollama --model nomic-embed-text
"""

import argparse
import json
import math
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = "http://" + OLLAMA_HOST

# ------------------------------------------------------------------ tokenizer
PHRASES = [  # applied in order, on folded lower-case text
    (r"\bu\.s\.(a\.)?(?=\W|$)|\bu\.s\b|\bunited states\b", " us "),
    (r"\bfederal reserve\b|\bfomc\b|\bthe fed\b", " fed "),
    (r"\bcentral bank of (the republic of )?turkey\b|\bcbrt\b|\btcmb\b|\bturkish central bank\b|\bturkey'?s central bank\b",
     " turkey centralbank "),
    (r"\bcentral banks?\b", " centralbank "),
    (r"\binterest rates?\b|\bpolicy rates?\b|\bbenchmark rates?\b|\bone-week repo\b", " rate "),
    (r"\bbasis points?\b|\bbps?\b", " bp "),
    (r"\bstrait of hormuz\b", " hormuz "),
    (r"\bnon-?farm payrolls?\b|\bjobs report\b|\blabor market\b|\blabour market\b", " jobs "),
    (r"\bconsumer prices?\b|\bprice growth\b", " inflation "),
    (r"\btreasury yields?\b|\bbond yields?\b", " yield "),
    (r"\bcurrent account\b", " currentaccount "),
    (r"\bforeign (exchange|currency) reserves?\b|\bfx reserves?\b", " reserves "),
    (r"\bcarry trade\b", " carrytrade "),
    (r"\brecord low\b|\ball-time low\b", " recordlow "),
    (r"\bsell-?off\b", " selloff "),
    (r"\bcease-?fire\b", " ceasefire "),
    (r"\brating agenc(y|ies)\b|\bmoody'?s\b|\bfitch\b|\bs&p global\b", " ratingagency "),
    (r"\bwall street\b", " wallstreet "),
    (r"\bwhite house\b", " whitehouse "),
    (r"\bearly elections?\b|\bsnap elections?\b", " election "),
]
STOP = set("""a an the of to in on at by for with from as is are was were be been being it its this that these those
and or but if then than so not no nor into onto over under after before amid amidst about against between during
via per up down out off again further once here there when where why how all any both each few more most other some
such only own same very can will just should now new says said say year years week weeks day days month months
today yesterday tuesday monday wednesday thursday friday saturday sunday percent pct how what who whom which s t
has have had having do does did his her their our your we you they he she them him us_ its' report reports
update updates live news january february march april may june july august september october
november december jan feb mar apr jun jul aug sep sept oct nov dec""".split())
CONCEPTS = {  # token after stemming -> concept token
    "hike": "hike", "raise": "hike", "tighten": "hike", "tightening": "hike", "hawkish": "hawk", "hawk": "hawk",
    "cut": "cut", "lower": "cut", "ease": "cut", "easing": "cut", "dovish": "dove", "dove": "dove",
    "pause": "hold", "hold": "hold", "unchanged": "hold", "steady": "hold",
    "oil": "energy", "crude": "energy", "brent": "energy", "wti": "energy", "fuel": "energy", "gas": "energy",
    "energy": "energy", "diesel": "energy", "opec": "energy",
    "payroll": "jobs", "employment": "jobs", "job": "jobs", "jobs": "jobs", "hiring": "jobs", "unemployment": "unemployment",
    "jobless": "unemployment", "cpi": "inflation", "inflation": "inflation", "ppi": "inflation",
    "yield": "yield", "treasury": "yield", "treasuries": "yield", "bond": "yield",
    "turkey": "turkey", "turkiye": "turkey", "turkish": "turkey", "ankara": "turkey", "istanbul": "turkey",
    "us": "us", "usa": "us", "american": "us", "america": "us", "washington": "us",
    "fed": "fed", "warsh": "fed", "powell": "fed",
    "lira": "lira", "try": "lira", "dollar": "dollar", "greenback": "dollar", "usd": "dollar",
    "reserve": "reserves", "reserves": "reserves", "outflow": "outflow", "inflow": "inflow",
    "erdogan": "erdogan", "imamoglu": "opposition", "ozel": "opposition", "chp": "opposition", "opposition": "opposition",
    "arrest": "arrest", "detain": "arrest", "jail": "arrest", "jailed": "arrest", "probe": "probe",
    "investigation": "probe", "scandal": "scandal", "fraud": "scandal", "ponzi": "scandal",
    "weaken": "weaken", "weak": "weaken", "slump": "weaken", "plunge": "weaken", "tumble": "weaken", "slide": "weaken",
    "depreciation": "weaken", "strengthen": "strengthen", "strong": "strengthen", "rally": "strengthen",
    "rebound": "strengthen", "appreciation": "strengthen", "gain": "strengthen",
    "slide": "weaken", "slip": "weaken", "surge": "rise", "jump": "rise", "climb": "rise", "rise": "rise", "soar": "rise", "spike": "rise", "higher": "rise",
    "fall": "fall", "drop": "fall", "decline": "fall", "sink": "fall", "retreat": "fall", "slow": "fall", "cool": "fall",
    "downgrade": "downgrade", "upgrade": "upgrade", "sanction": "sanction", "tariff": "tariff",
    "election": "election", "vote": "election", "miss": "miss", "beat": "beat",
}
POLICY = {"hike", "cut", "hawk", "dove", "hold"}
_word = re.compile(r"[a-z0-9]+(?:[&'][a-z0-9]+)*")


def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return t.replace("ı", "i")


def stem(w: str) -> str:
    """Light stemmer: try plausible base forms and prefer one that is a known concept."""
    if len(w) <= 3 or w in CONCEPTS:
        return w
    dbl = lambda b: b[:-1] if len(b) >= 4 and b[-1] == b[-2] and b[-1] not in "lsz" else b   # cutting -> cut
    cands = []
    if w.endswith(("ies", "ied")):
        cands.append(w[:-3] + "y")
    elif w.endswith("ing"):
        b = w[:-3]; cands += [b, b + "e", dbl(b)]
    elif w.endswith("ed"):
        b = w[:-2]; cands += [b, w[:-1], dbl(b)]                   # hiked -> hike, jailed -> jail
    elif w.endswith("es"):
        cands += [w[:-1], w[:-2]]                                  # prices -> price, taxes -> tax
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        cands.append(w[:-1])
    cands = [c for c in cands if len(c) >= 3]
    for c in cands:
        if c in CONCEPTS:
            return c
    if not cands:
        return w
    if w.endswith("ing") or (w.endswith("ed") and not w.endswith("ied")):
        return dbl(cands[0])
    if w.endswith("es"):
        return w[:-2] if w.endswith(("sses", "shes", "ches", "xes", "zes")) else w[:-1]
    return cands[0]


def tokenize(text: str):
    """Text -> list of tokens (unigrams, concept tokens, context tokens, bigrams), per sentence."""
    out = []
    t = fold(text)
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t)
    for sent in re.split(r"[.;:!?\n]|\s-\s", t):
        words = [w for w in _word.findall(sent.replace("'s", "")) if w not in STOP and not w.isdigit()]
        toks = [CONCEPTS.get(stem(w), stem(w)) for w in words]
        toks = [x for x in toks if x not in STOP]
        if not toks:
            continue
        s = set(toks)
        ctx = []
        has_tr = bool(s & {"turkey", "lira"})
        has_us = bool(s & {"us", "fed", "yield", "dollar", "wallstreet", "whitehouse"})
        for p in sorted(POLICY & s):
            if has_tr and "centralbank" in s:
                ctx.append(f"@tr_{p}")               # Turkish central bank action
            elif "fed" in s or (has_us and not has_tr):
                ctx.append(f"@us_{p}")               # Fed / US rates
            elif has_tr:
                ctx.append(f"@tr_{p}")
        out += toks + ctx + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
    return out


# ------------------------------------------------------------------ prompts
def load_prompts(path=None):
    p = Path(path) if path else HERE / "prompts.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: data[k] for k in ("up", "down")}


def prompt_text(pr):
    return pr["text"] + ". " + pr.get("keywords", "")


# ------------------------------------------------------------------ back-ends
class TfidfScorer:
    name = "tfidf"

    def __init__(self, corpus):
        docs = [Counter(tokenize(d)) for d in corpus]
        n = len(docs)
        df = Counter(t for d in docs for t in d)
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}

    def vec(self, text):
        tf = Counter(tokenize(text))
        v = {t: (1 + math.log(c)) * self.idf.get(t, 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def sims(self, query, docs):
        q = self.vec(query)
        return [sum(q.get(t, 0.0) * x for t, x in self.vec(d).items()) for d in docs]

    def matched_terms(self, query, doc, k=4):
        q, d = self.vec(query), self.vec(doc)
        common = sorted(((q[t] * d[t], t) for t in q.keys() & d.keys()), reverse=True)
        return [t.lstrip("@").replace("_", " ") for _, t in common[:k]]


class OllamaScorer:
    name = "ollama"

    def __init__(self, model="nomic-embed-text", host=OLLAMA_HOST, timeout=60):
        self.model, self.host, self.timeout = model, host, timeout
        self.qp, self.dp = ("search_query: ", "search_document: ") if "nomic" in model else ("", "")

    @staticmethod
    def available(host=OLLAMA_HOST):
        try:
            return requests.get(f"{host}/api/tags", timeout=1.5).ok
        except Exception:
            return False

    def embed(self, texts):
        r = requests.post(f"{self.host}/api/embed", json={"model": self.model, "input": texts}, timeout=self.timeout)
        if r.status_code == 404:                     # older Ollama: one text per call
            out = []
            for t in texts:
                rr = requests.post(f"{self.host}/api/embeddings", json={"model": self.model, "prompt": t},
                                   timeout=self.timeout)
                rr.raise_for_status()
                out.append(rr.json()["embedding"])
            return out
        r.raise_for_status()
        return r.json()["embeddings"]

    def sims(self, query, docs):
        vecs = self.embed([self.qp + query] + [self.dp + d for d in docs])
        q, ds = vecs[0], vecs[1:]

        def cos(a, b):
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(x * x for x in b)) or 1.0
            return sum(x * y for x, y in zip(a, b)) / (na * nb)
        return [cos(q, d) for d in ds]

    def matched_terms(self, query, doc, k=4):
        return []


def make_scorer(backend, corpus, model="nomic-embed-text", log=print):
    if backend in ("ollama", "auto"):
        if OllamaScorer.available():
            log(f"Relevance: Ollama embeddings ({model}) at {OLLAMA_HOST}")
            return OllamaScorer(model)
        if backend == "ollama":
            log(f"Relevance: Ollama not reachable at {OLLAMA_HOST}; using the tokenizer (TF-IDF) instead")
    log("Relevance: tokenizer + TF-IDF cosine")
    return TfidfScorer(corpus)


# ------------------------------------------------------------------ ranking
def score_news(news: dict, direction: str, top=5, backend="auto", model="nomic-embed-text",
               prompts_path=None, log=print):
    """Adds sim_up, sim_down, relevance and selected flags to news['items']; returns news."""
    items = news.get("items") or []
    if not items:
        return news
    prompts = load_prompts(prompts_path)
    docs = [f"{it['title']}. {it.get('why', '')}" for it in items]
    queries = {k: prompt_text(v) for k, v in prompts.items()}
    scorer = make_scorer(backend, docs + list(queries.values()), model, log)
    try:
        sims = {k: scorer.sims(q, docs) for k, q in queries.items()}
    except Exception as ex:                          # e.g. model not pulled
        log(f"Relevance: {scorer.name} failed ({type(ex).__name__}: {ex}); using TF-IDF")
        scorer = TfidfScorer(docs + list(queries.values()))
        sims = {k: scorer.sims(q, docs) for k, q in queries.items()}

    other = "down" if direction == "up" else "up"
    for i, it in enumerate(items):
        it["sim_up"], it["sim_down"] = round(sims["up"][i], 4), round(sims["down"][i], 4)
        it["relevance"] = it[f"sim_{direction}"]
        it["lean"] = round(it[f"sim_{direction}"] - it[f"sim_{other}"], 4)   # >0: fits this day type better
        it["matched"] = scorer.matched_terms(queries[direction], docs[i])
    order = sorted(range(len(items)), key=lambda i: (-items[i]["relevance"], -items[i]["lean"]))
    for rank, i in enumerate(order, 1):
        items[i]["rel_rank"] = rank
        items[i]["selected"] = rank <= top
    news["items"] = [items[i] for i in order]
    news["relevance"] = {
        "direction": direction, "label": prompts[direction]["label"], "top": min(top, len(items)),
        "method": ("Ollama embeddings, " + scorer.model) if scorer.name == "ollama" else "tokenizer + TF-IDF cosine",
        "prompt": prompts[direction]["text"],
    }
    return news


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Score headlines against the up/down day prompts.")
    ap.add_argument("news", help="news.json (from news_fetch.py or curated)")
    ap.add_argument("--direction", choices=["up", "down"], required=True)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--backend", choices=["auto", "tfidf", "ollama"], default="auto")
    ap.add_argument("--model", default="nomic-embed-text")
    ap.add_argument("--prompts", help="prompts JSON (default: prompts.json next to this file)")
    ap.add_argument("--show-tokens", action="store_true", help="print the tokens of each headline")
    a = ap.parse_args()
    n = score_news(json.loads(Path(a.news).read_text(encoding="utf-8")), a.direction, a.top, a.backend, a.model, a.prompts)
    print(f"\n{n['relevance']['label']}  ·  {n['relevance']['method']}\n")
    print(f"{'#':>2}  {'sim_up':>7} {'sim_dn':>7}  sel  headline")
    for it in n["items"]:
        print(f"{it['rel_rank']:>2}  {it['sim_up']:7.3f} {it['sim_down']:7.3f}  {'*' if it['selected'] else ' '}   "
              f"{it['title'][:88]}" + (f"   [{', '.join(it['matched'])}]" if it["matched"] else ""))
        if a.show_tokens:
            print("      ", " ".join(t for t in tokenize(it["title"]) if "_" not in t or t.startswith("@")))
