"""Função serverless (Vercel, runtime Python): GET /api/dados

Filtros opcionais: ?prestadora=A&prestadora=B&servico=...&indicador=...&de=2025-01&ate=2025-12
Retorna filtros disponíveis, série mensal por indicador, ranking por prestadora e KPIs.
"""
from http.server import BaseHTTPRequestHandler
from collections import defaultdict
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import json

DATA = Path(__file__).resolve().parent.parent / "data"


def carregar():
    regs = json.loads((DATA / "registros.json").read_text(encoding="utf-8"))
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    return regs, meta


def media(xs):
    return round(sum(xs) / len(xs), 2) if xs else None


def montar(regs, meta, q):
    # regs: [periodo, prestadora, servico, indicador, valor]
    opcoes = {
        "prestadoras": sorted({r[1] for r in regs}),
        "servicos": sorted({r[2] for r in regs}),
        "indicadores": sorted({r[3] for r in regs}),
        "periodos": sorted({r[0] for r in regs}),
    }
    pre, ser, ind = set(q.get("prestadora", [])), set(q.get("servico", [])), set(q.get("indicador", []))
    de, ate = (q.get("de") or [""])[0], (q.get("ate") or [""])[0]
    f = [r for r in regs
         if (not pre or r[1] in pre) and (not ser or r[2] in ser) and (not ind or r[3] in ind)
         and (not de or r[0] >= de) and (not ate or r[0] <= ate)]

    serie = defaultdict(lambda: defaultdict(list))   # indicador -> periodo -> valores
    rank = defaultdict(lambda: defaultdict(list))    # indicador -> prestadora -> valores
    for p, e, s, i, v in f:
        serie[i][p].append(v)
        rank[i][e].append(v)

    return {
        "meta": meta,
        "opcoes": opcoes,
        "kpis": {"registros": len(f), "prestadoras": len({r[1] for r in f}),
                 "indicadores": len(serie), "valor_medio": media([r[4] for r in f])},
        "serie": {i: [{"periodo": p, "valor": media(v)} for p, v in sorted(d.items())]
                  for i, d in serie.items()},
        "ranking": {i: sorted(({"prestadora": e, "valor": media(v)} for e, v in d.items()),
                              key=lambda x: -(x["valor"] or 0))
                    for i, d in rank.items()},
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            regs, meta = carregar()
            body = json.dumps(montar(regs, meta, parse_qs(urlparse(self.path).query)),
                              ensure_ascii=False).encode()
            self.send_response(200)
        except Exception as exc:  # noqa: BLE001
            body = json.dumps({"erro": str(exc)}).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # dados mudam a cada 2 dias: cache de CDN de 1h, revalida em segundo plano
        self.send_header("Cache-Control", "public, s-maxage=3600, stale-while-revalidate=86400")
        self.end_headers()
        self.wfile.write(body)
