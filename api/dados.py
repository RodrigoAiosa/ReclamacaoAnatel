"""Função serverless (Vercel, Python): GET /api/dados

Parâmetros (todos opcionais; repetíveis): servico, prestadora, uf, de=YYYY-MM, ate=YYYY-MM, metrica
Lê os agregados gerados por scripts/etl.py (data/*.json.gz) e devolve KPIs, série mensal e rankings.
"""
from http.server import BaseHTTPRequestHandler
from collections import defaultdict
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import gzip, json

DATA = Path(__file__).resolve().parent.parent / "data"
METRICAS = {
    "reclamacoes": "Reclamações (volume)",
    "pct_respondidas": "% respondidas",
    "pct_resolvidas": "% resolvidas (entre avaliadas)",
    "pct_reabertas": "% reabertas",
    "nota": "Nota média do consumidor (1–5)",
    "prazo": "Prazo médio de resposta (dias)",
}
_cache = {}


def _tabela(nome):
    if nome not in _cache:
        with gzip.open(DATA / f"{nome}.json.gz", "rt", encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        for r in rows:                      # campos vazios na origem -> "N/I"
            for i in range(4):
                r[i] = r[i] or "N/I"
        _cache[nome] = rows
    return _cache[nome]


def _meta():
    return json.loads((DATA / "meta.json").read_text(encoding="utf-8"))


def valor(m, metrica):
    """m = [dem, rec, resp, resol, aval, reab, nota_n, nota_sum, prazo_n, prazo_sum]"""
    dem, rec, resp, resol, aval, reab, nn, ns, pn, ps = m
    div = lambda a, b, k=1: round(k * a / b, 2) if b else None
    return {"reclamacoes": rec, "pct_respondidas": div(resp, dem, 100), "pct_resolvidas": div(resol, aval, 100),
            "pct_reabertas": div(reab, dem, 100), "nota": div(ns, nn), "prazo": div(ps, pn)}[metrica]


def _soma(acc, row):
    for i, v in enumerate(row):
        acc[i] += v


def montar(q):
    metrica = (q.get("metrica") or ["reclamacoes"])[0]
    if metrica not in METRICAS:
        metrica = "reclamacoes"
    sv, pr, uf = set(q.get("servico", [])), set(q.get("prestadora", [])), set(q.get("uf", []))
    de, ate = (q.get("de") or [""])[0], (q.get("ate") or [""])[0]
    ok = lambda mes, s, p: (not sv or s in sv) and (not pr or p in pr) and (not de or mes >= de) and (not ate or mes <= ate)

    A, B = _tabela("por_uf"), _tabela("por_assunto")   # [mes, servico, prestadora, uf|assunto, *10 medidas]
    z = lambda: [0.0] * 10
    serie, rank_p, rank_u, rank_a, total = defaultdict(z), defaultdict(z), defaultdict(z), defaultdict(z), z()
    for r in A:
        if ok(r[0], r[1], r[2]) and (not uf or r[3] in uf):
            for acc in (serie[r[0]], rank_p[r[2]], rank_u[r[3]], total):
                _soma(acc, r[4:])
    for r in B:
        if ok(r[0], r[1], r[2]) and not uf:   # base de assunto não tem UF
            _soma(rank_a[r[3]], r[4:])

    def top(d, n=12, minimo=200):
        itens = [{"nome": k, "valor": valor(v, metrica), "demandas": int(v[0])} for k, v in d.items() if v[0] >= minimo]
        itens = [i for i in itens if i["valor"] is not None]
        return sorted(itens, key=lambda x: -x["valor"])[:n]

    opcoes = {"servicos": sorted({r[1] for r in A}), "prestadoras": sorted({r[2] for r in A}),
              "ufs": sorted({r[3] for r in A}), "periodos": sorted({r[0] for r in A})}
    return {
        "meta": _meta(), "metricas": METRICAS, "metrica": metrica, "opcoes": opcoes,
        "kpis": {"demandas": int(total[0]), **{k: valor(total, k) for k in METRICAS}},
        "serie": [{"periodo": k, "valor": valor(v, metrica)} for k, v in sorted(serie.items())],
        "ranking_prestadora": top(rank_p), "ranking_uf": top(rank_u, 27, 50),
        "ranking_assunto": top(rank_a, 12), "assunto_disponivel": not uf,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body, status = json.dumps(montar(parse_qs(urlparse(self.path).query)), ensure_ascii=False).encode(), 200
        except Exception as exc:  # noqa: BLE001
            body, status = json.dumps({"erro": f"{type(exc).__name__}: {exc}"}).encode(), 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "public, s-maxage=3600, stale-while-revalidate=86400"
                         if status == 200 else "no-store")
        self.end_headers()
        self.wfile.write(body)
