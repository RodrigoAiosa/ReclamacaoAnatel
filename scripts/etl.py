"""ETL rápido (DuckDB): AnatelConsumidorReclamacoesCSV (2,4 GB, 1 linha por demanda)
-> agregados pequenos em data/ (JSON gzip) que a API na Vercel consome.

    DATA_URL=https://.../indicadores_qualidade_atendimento.zip python scripts/etl.py
    python scripts/etl.py --file arquivo.zip|.csv
Se o ZIP não mudou desde a última execução (sha256), sai sem reprocessar.
"""
from __future__ import annotations
import argparse, gzip, hashlib, json, os, shutil, subprocess, sys, tempfile, time, urllib.request, zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MEDIDAS = ["dem", "rec", "resp", "resol", "aval", "reab", "nota_n", "nota_sum", "prazo_n", "prazo_sum"]
T0 = time.time()
log = lambda *a: print(f"[{time.time() - T0:5.0f}s]", *a, flush=True)

BASE_SQL = """
CREATE TABLE base AS
SELECT substr(DATA_ABERTURA, 1, 7)                       AS mes,
       coalesce(nullif(trim(SERVICO), ''), 'N/I')         AS servico,
       coalesce(nullif(upper(trim(PRESTADORA)), ''), 'N/I') AS prestadora,
       coalesce(nullif(trim(UF), ''), 'N/I')              AS uf,
       coalesce(nullif(trim(ASSUNTO), ''), 'N/I')         AS assunto,
       TIPO_ATENDIMENTO = 'Reclamação'                    AS rec,
       RESPONDIDA = 'S'                                   AS resp,
       RESOLVIDA = 'S'                                    AS resol,
       RESOLVIDA IN ('S', 'N')                            AS aval,
       REABERTA = 'S'                                     AS reab,
       try_cast(NOTA_CONSUMIDOR AS DOUBLE)                AS nota,
       try_cast(PrazoResposta AS DOUBLE)                  AS prazo
FROM read_csv('{csv}', delim=';', header=true, all_varchar=true, encoding='{enc}',
              ignore_errors=true, parallel=true)
WHERE regexp_matches(DATA_ABERTURA, '^[0-9]{{4}}-[0-9]{{2}}')
"""
AGG = """
SELECT {dims}, count(*) dem, sum(rec::INT) rec, sum(resp::INT) resp, sum(resol::INT) resol,
       sum(aval::INT) aval, sum(reab::INT) reab, count(nota) nota_n, coalesce(sum(nota),0) nota_sum,
       count(prazo) FILTER (WHERE resp) prazo_n, coalesce(sum(prazo) FILTER (WHERE resp),0) prazo_sum
FROM base GROUP BY ALL ORDER BY ALL
"""


def obter_zip(args) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="anatel_"))
    if args.file:
        return Path(args.file)
    url = os.environ.get("DATA_URL") or sys.exit("Defina DATA_URL ou use --file.")
    dest = tmp / "dados.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 22)
    log(f"download ok: {dest.stat().st_size >> 20} MB")
    return dest


def extrair_csv(p: Path) -> Path:
    if p.suffix.lower() != ".zip":
        return p
    out = Path(tempfile.mkdtemp(prefix="anatel_csv_"))
    if shutil.which("unzip"):
        subprocess.run(["unzip", "-q", "-o", str(p), "-d", str(out)], check=True)
    else:
        zipfile.ZipFile(p).extractall(out)
    csvs = sorted(out.rglob("*.csv"), key=lambda x: x.stat().st_size, reverse=True)
    if not csvs:
        sys.exit("Nenhum CSV dentro do ZIP.")
    log(f"extraído: {csvs[0].name} ({csvs[0].stat().st_size >> 20} MB)")
    return csvs[0]


def gravar(nome: str, cols: list[str], rows: list[tuple]) -> int:
    path = DATA / f"{nome}.json.gz"
    payload = json.dumps({"cols": cols, "rows": [[round(v, 2) if isinstance(v, float) else v for v in r] for r in rows]},
                         ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.GzipFile(path, "wb", mtime=0) as f:   # mtime=0 => saída determinística (sem commits à toa)
        f.write(payload)
    log(f"{nome}: {len(rows):,} linhas, {path.stat().st_size >> 10} KB".replace(",", "."))
    return len(rows)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--file"); ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    zp = obter_zip(args)
    digest = hashlib.sha256(zp.read_bytes()).hexdigest() if zp.stat().st_size < (1 << 31) else "grande"
    meta_p = DATA / "meta.json"
    if not args.force and meta_p.exists() and json.loads(meta_p.read_text()).get("sha256") == digest:
        log("arquivo inalterado desde a última execução; nada a fazer."); return

    csv = extrair_csv(zp)
    con = duckdb.connect()
    con.execute(f"SET threads={os.cpu_count() or 2}")
    for enc in ("utf-8", "latin-1"):
        try:
            con.execute("DROP TABLE IF EXISTS base"); con.execute(BASE_SQL.format(csv=csv, enc=enc)); break
        except duckdb.Error as e:
            log(f"leitura com {enc} falhou: {str(e)[:120]}")
    n = con.execute("SELECT count(*) FROM base").fetchone()[0]
    log(f"base carregada: {n:,} linhas".replace(",", "."))
    if n == 0:
        sys.exit("Nenhuma linha válida: o layout do arquivo mudou?")

    d1 = ["mes", "servico", "prestadora", "uf"]
    d2 = ["mes", "servico", "prestadora", "assunto"]
    n1 = gravar("por_uf", d1 + MEDIDAS, con.execute(AGG.format(dims=", ".join(d1))).fetchall())
    n2 = gravar("por_assunto", d2 + MEDIDAS, con.execute(AGG.format(dims=", ".join(d2))).fetchall())
    mn, mx = con.execute("SELECT min(mes), max(mes) FROM base").fetchone()
    meta_p.write_text(json.dumps({
        "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origem": os.environ.get("DATA_URL", str(args.file)), "sha256": digest,
        "registros_origem": n, "periodo": [mn, mx], "linhas_agregadas": n1 + n2, "medidas": MEDIDAS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"concluído ({mn} → {mx})")


if __name__ == "__main__":
    main()
