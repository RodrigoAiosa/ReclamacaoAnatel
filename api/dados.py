"""Função serverless (Vercel, Python): GET /api/dados

Filtros opcionais: ano=YYYY, mes=MM, servico, prestadora, uf
Lê os agregados de scripts/etl.py (data/*.json.gz) e devolve KPIs, satisfação, série mensal e rankings
já com os dados que alimentam os tooltips do dashboard.
"""
from http.server import BaseHTTPRequestHandler
from collections import defaultdict
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import gzip, json

DATA = Path(__file__).resolve().parent.parent / "data"
NM = 15  # nº de medidas: dem rec resp resol aval reab nota_n nota_sum prazo_n prazo_sum n1..n5
NOTAS = [(5, "Muito satisf."), (4, "Satisfeito"), (3, "Indiferente"), (2, "Insatisfeito"), (1, "Muito insat.")]
_cache = {}


def _tabela(nome):
    if nome not in _cache:
        with gzip.open(DATA / f"{nome}.json.gz", "rt", encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        for r in rows:
            for i in range(4):
                r[i] = r[i] or "N/I"
            r.extend([0] * (4 + NM - len(r)))        # tolera dados no formato antigo
        _cache[nome] = rows
    return _cache[nome]


def _dias():
    """Agregado diário (opcional): [dia, servico, prestadora, dem, resp, resol, aval]."""
    if "dia" not in _cache:
        try:
            with gzip.open(DATA / "por_dia.json.gz", "rt", encoding="utf-8") as f:
                _cache["dia"] = json.load(f)["rows"]
        except FileNotFoundError:
            _cache["dia"] = []
    return _cache["dia"]


def _div(a, b, k=1.0, d=2):
    return round(k * a / b, d) if b else None


def sat(v):
    """Satisfação a partir do vetor de medidas (notas 1–5)."""
    n = {k: v[9 + k] for k in range(1, 6)}
    tot = sum(n.values())
    pos, neg = _div(n[4] + n[5], tot, 100, 1), _div(n[1] + n[2], tot, 100, 1)
    saldo = round(pos - neg, 1) if tot else None
    if not tot:
        nivel = "Sem dados"
    elif saldo < 0:
        nivel = "Crítico"
    elif neg >= 20:
        nivel = "Atenção"
    elif saldo >= 50:
        nivel = "Excelente"
    else:
        nivel = "Bom"
    pred = max(NOTAS, key=lambda x: n[x[0]])[1] if tot else "–"
    return {"avaliacoes": int(tot), "nota": _div(v[7], v[6]), "satisfeitos": pos, "insatisfeitos": neg,
            "saldo": saldo, "nivel": nivel, "predominante": pred,
            "dist": [{"nota": k, "label": lb, "n": int(n[k]), "pct": _div(n[k], tot, 100, 1) or 0} for k, lb in NOTAS]}


def resumo(v):
    dem, rec, resp, resol, aval = v[0], v[1], v[2], v[3], v[4]
    return {"total": int(dem), "respondidas": int(resp), "nao_respondidas": int(dem - resp),
            "resolvidas": int(resol), "nao_resolvidas": int(aval - resol), "reabertas": int(v[5]),
            "pct_respondidas": _div(resp, dem, 100), "pct_resolvidas": _div(resol, aval, 100),
            "tmr": _div(v[9], v[8], 1, 1)}


def _soma(acc, row):
    for i in range(NM):
        acc[i] += row[4 + i]


def montar(q):
    g = lambda k: (q.get(k) or [""])[0]
    ano, mes, sv, pr, uf = g("ano"), g("mes"), g("servico"), g("prestadora"), g("uf")
    A, B = _tabela("por_uf"), _tabela("por_assunto")   # [mes, servico, prestadora, uf|assunto, *medidas]
    base = lambda r: (not sv or r[1] == sv) and (not pr or r[2] == pr)
    no_ano = lambda r: not ano or r[0][:4] == ano
    no_mes = lambda r: not mes or r[0][5:7] == mes
    z = lambda: [0.0] * NM

    tot, serie = z(), defaultdict(z)
    por_op, por_uf, op_serv, op_uf = defaultdict(z), defaultdict(z), defaultdict(lambda: defaultdict(float)), defaultdict(z)
    for r in A:
        if not base(r) or not no_ano(r):
            continue
        if not uf or r[3] == uf:
            _soma(serie[r[0]], r)                     # série: ignora filtro de mês
        if not no_mes(r):
            continue
        _soma(por_uf[r[3]], r)                        # regional: ignora filtro de UF
        if not uf or r[3] == uf:
            _soma(tot, r); _soma(por_op[r[2]], r)
            op_serv[r[2]][r[1]] += r[4]
    ass, ass_serv = defaultdict(z), defaultdict(lambda: defaultdict(float))
    for r in B:
        if base(r) and no_ano(r) and no_mes(r):
            _soma(ass[r[3]], r); ass_serv[r[3]][r[1]] += r[4]

    def servs(d, total):
        return [{"nome": k, "n": int(n), "pct": _div(n, total, 100)} for k, n in sorted(d.items(), key=lambda x: -x[1]) if n]

    ops = sorted(por_op.items(), key=lambda x: -x[1][0])
    top_op = [{"nome": k, "total": int(v[0]), "sat": sat(v), "servicos": servs(op_serv[k], v[0]), **resumo(v)} for k, v in ops[:20]]
    assuntos = [{"nome": k, "total": int(v[0]), "servicos": servs(ass_serv[k], v[0])}
                for k, v in sorted(ass.items(), key=lambda x: -x[1][0])[:10]]
    regional = [{"nome": k, "total": int(v[0]), "sat": sat(v), **resumo(v)}
                for k, v in sorted(por_uf.items(), key=lambda x: -x[1][0]) if v[0]]
    meses = [{"periodo": k, "nota": _div(v[7], v[6]), **resumo(v)} for k, v in sorted(serie.items())]
    # últimos dias de cada mês (tooltip da linha mensal) — respeita serviço/operadora; UF não existe no diário
    dia_acc = defaultdict(lambda: [0, 0, 0, 0])
    for r in _dias():
        if base(r) and no_ano(r):
            a = dia_acc[r[0]]
            for i in range(4):
                a[i] += r[3 + i]
    dias = defaultdict(list)
    for d in sorted(dia_acc, reverse=True):
        lst = dias[d[:7]]
        if len(lst) < 7:
            v = dia_acc[d]
            lst.append({"dia": d, "total": v[0], "respondida": v[1], "resolvida": v[2], "pct": _div(v[2], v[0], 100, 2)})
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    return {
        "meta": meta,
        "opcoes": {"anos": sorted({r[0][:4] for r in A}, reverse=True), "servicos": sorted({r[1] for r in A}),
                   "prestadoras": sorted({r[2] for r in A}), "ufs": sorted({r[3] for r in A})},
        "kpis": resumo(tot), "sat": sat(tot), "serie": meses,
        "dias": dias, "operadoras": top_op, "assuntos": assuntos, "regional": regional,
        "assunto_filtra_uf": bool(uf),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body, status = json.dumps(montar(parse_qs(urlparse(self.path).query)), ensure_ascii=False).encode(), 200
        except Exception as exc:  # noqa: BLE001
            body, status = json.dumps({"erro": f"{type(exc).__name__}: {exc}"}).encode(), 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "public, s-maxage=3600, stale-while-revalidate=86400" if status == 200 else "no-store")
        self.end_headers()
        self.wfile.write(body)
