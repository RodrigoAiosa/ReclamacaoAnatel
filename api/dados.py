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


def _ultima_data():
    """Data de abertura mais recente presente nos dados (para a capa)."""
    if "ultima" not in _cache:
        _cache["ultima"] = max((r[0] for r in _dias()), default="")
    return _cache["ultima"]


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
            "pct_reabertas": _div(v[5], resp, 100),
            "tmr": _div(v[9], v[8], 1, 1)}


def _soma(acc, row):
    for i in range(NM):
        acc[i] += row[4 + i]


def _bloco_servicos(A, B, ano, mes, pr, uf, sv, base, no_ano, no_mes, z):
    """Dados da aba Serviços e Assuntos. As comparações entre serviços ignoram o filtro de serviço
    (senão só sobraria uma barra); o mapa operadora × assunto ignora o filtro de operadora."""
    por_sv, serie_sv = defaultdict(z), defaultdict(lambda: defaultdict(float))
    for r in A:
        if (pr and r[2] != pr) or (uf and r[3] != uf) or not no_ano(r):
            continue
        serie_sv[r[1]][r[0]] += r[4]
        if no_mes(r):
            _soma(por_sv[r[1]], r)
    lista = [{"nome": k, "total": int(v[0]), "sat": sat(v), **resumo(v)}
             for k, v in sorted(por_sv.items(), key=lambda x: -x[1][0]) if v[0]]
    nomes = [x["nome"] for x in lista]
    periodos = sorted({p for d in serie_sv.values() for p in d})
    evolucao = {"periodos": periodos, "series": [{"nome": n, "valores": [int(serie_sv[n].get(p, 0)) for p in periodos]} for n in nomes]}

    a_sv, a_tot, sv_tot = defaultdict(lambda: defaultdict(float)), defaultdict(float), defaultdict(float)
    heat, op_tot, as_tot = defaultdict(lambda: [0.0, 0.0, 0.0]), defaultdict(float), defaultdict(float)
    for r in B:
        if not no_ano(r) or not no_mes(r):
            continue
        if not pr or r[2] == pr:                             # assunto × serviço respeita operadora
            a_sv[r[3]][r[1]] += r[4]; a_tot[r[3]] += r[4]; sv_tot[r[1]] += r[4]
        if not sv or r[1] == sv:                             # mapa respeita serviço, ignora operadora
            h = heat[(r[2], r[3])]
            h[0] += r[4]; h[1] += r[7]; h[2] += r[8]         # dem, resol, aval
            op_tot[r[2]] += r[4]; as_tot[r[3]] += r[4]
    top_as = [k for k, _ in sorted(a_tot.items(), key=lambda x: -x[1])[:8]]
    assunto_servico = {"assuntos": top_as, "servicos": nomes,
                       "valores": [[int(a_sv[a].get(n, 0)) for n in nomes] for a in top_as], "totais": [int(a_tot[a]) for a in top_as],
                       "totais_servico": [int(sv_tot[n]) for n in nomes]}
    ops = [k for k, _ in sorted(op_tot.items(), key=lambda x: -x[1])[:10]]
    ass = [k for k, _ in sorted(as_tot.items(), key=lambda x: -x[1])[:8]]
    celulas = []
    for o in ops:
        for a in ass:
            d, rs, av = heat.get((o, a), (0, 0, 0))
            celulas.append({"op": o, "assunto": a, "total": int(d), "pct_op": _div(d, op_tot[o], 100),
                            "pct_resolvidas": _div(rs, av, 100)})
    return {"lista": lista, "evolucao": evolucao, "assunto_servico": assunto_servico,
            "mapa": {"operadoras": ops, "assuntos": ass, "totais_op": [int(op_tot[o]) for o in ops], "celulas": celulas}}


def _principais(A, n=8):
    """Operadoras de maior volume nos últimos 12 meses da base (independe dos filtros) — atalhos com ícone.
    Ignora 'N/I' e empresas marcadas como inativas."""
    if "principais" not in _cache:
        meses = sorted({r[0] for r in A}, reverse=True)[:12]
        tot = defaultdict(float)
        for r in A:
            if r[0] in meses:
                tot[r[2]] += r[4]
        _cache["principais"] = [k for k, _ in sorted(tot.items(), key=lambda x: -x[1]) if k != "N/I" and "INATIV" not in k.upper()][:n]
    return _cache["principais"]


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
    servicos = _bloco_servicos(A, B, ano, mes, pr, uf, sv, base, no_ano, no_mes, z)
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    try:                                        # última checagem do workflow (opcional)
        meta["verificado_em"] = json.loads((DATA / "verificacao.json").read_text(encoding="utf-8"))["verificado_em"]
    except (FileNotFoundError, KeyError, ValueError):
        meta["verificado_em"] = meta.get("atualizado_em")
    return {
        "meta": meta,
        "opcoes": {"anos": sorted({r[0][:4] for r in A}, reverse=True), "servicos": sorted({r[1] for r in A}),
                   "prestadoras": sorted({r[2] for r in A}), "principais": _principais(A), "ufs": sorted({r[3] for r in A})},
        "kpis": resumo(tot), "sat": sat(tot), "serie": meses,
        "dias": dias, "ultima_data": _ultima_data(), "operadoras": top_op, "assuntos": assuntos, "regional": regional,
        "assunto_filtra_uf": bool(uf), "servicos": servicos,
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
