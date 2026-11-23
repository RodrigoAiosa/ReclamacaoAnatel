"""ETL rápido (DuckDB): AnatelConsumidorReclamacoesCSV (2,4 GB, 1 linha por demanda)
-> agregados pequenos em data/ (JSON gzip) que a API na Vercel consome.

    DATA_URL=https://.../indicadores_qualidade_atendimento.zip python scripts/etl.py
    python scripts/etl.py --file arquivo.zip|.csv
Se o ZIP não mudou desde a última execução (sha256), sai sem reprocessar.
"""
from __future__ import annotations
import argparse, csv, gzip, hashlib, json, os, shutil, subprocess, sys, tempfile, time, urllib.request, zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOG_CSV = ROOT / "logs" / "atualizacoes.csv"
COLUNAS_LOG = [
    "data_hora_execucao_utc", "data_hora_atualizacao_utc", "status", "gatilho", "run_id", "run_numero", "run_url", "commit_sha",
    "origem_url", "sha256_zip", "tamanho_zip_mb", "registros_origem", "linhas_agregadas", "periodo_inicio", "periodo_fim",
    "ultima_data_abertura", "duracao_segundos", "versao_etl", "mensagem"]
MEDIDAS = ["dem", "rec", "resp", "resol", "aval", "reab", "nota_n", "nota_sum", "prazo_n", "prazo_sum",
           "n1", "n2", "n3", "n4", "n5"]
VERSAO = 3   # aumente quando o formato dos agregados mudar (força reprocessar)
T0 = time.time()
log = lambda *a: print(f"[{time.time() - T0:5.0f}s]", *a, flush=True)

BASE_SQL = """
CREATE TABLE base AS
SELECT substr(DATA_ABERTURA, 1, 7)                       AS mes,
       substr(DATA_ABERTURA, 1, 10)                      AS dia,
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
AGG_DIA = """
SELECT dia, servico, prestadora, count(*) dem, sum(resp::INT) resp, sum(resol::INT) resol, sum(aval::INT) aval
FROM base WHERE regexp_matches(dia, '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') GROUP BY ALL ORDER BY ALL
"""
AGG = """
SELECT {dims}, count(*) dem, sum(rec::INT) rec, sum(resp::INT) resp, sum(resol::INT) resol,
       sum(aval::INT) aval, sum(reab::INT) reab, count(nota) nota_n, coalesce(sum(nota),0) nota_sum,
       count(prazo) FILTER (WHERE resp) prazo_n, coalesce(sum(prazo) FILTER (WHERE resp),0) prazo_sum,
       count(*) FILTER (WHERE nota = 1) n1, count(*) FILTER (WHERE nota = 2) n2, count(*) FILTER (WHERE nota = 3) n3,
       count(*) FILTER (WHERE nota = 4) n4, count(*) FILTER (WHERE nota = 5) n5
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


def marcar_verificacao(mudou: bool):
    """Registra que o workflow checou a fonte (mesmo sem dados novos)."""
    (DATA / "verificacao.json").write_text(json.dumps({
        "verificado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"), "dados_novos": mudou}), encoding="utf-8")


def registrar_log(st: dict):
    """Acrescenta uma linha em logs/atualizacoes.csv (cria o arquivo com cabeçalho se não existir)."""
    env = os.environ.get
    run_id, repo, srv = env("GITHUB_RUN_ID", ""), env("GITHUB_REPOSITORY", ""), env("GITHUB_SERVER_URL", "https://github.com")
    linha = {
        "data_hora_execucao_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gatilho": env("GITHUB_EVENT_NAME", "manual"), "run_id": run_id, "run_numero": env("GITHUB_RUN_NUMBER", ""),
        "run_url": f"{srv}/{repo}/actions/runs/{run_id}" if run_id else "", "commit_sha": env("GITHUB_SHA", "")[:7],
        "duracao_segundos": round(time.time() - T0), "versao_etl": VERSAO, **st}
    LOG_CSV.parent.mkdir(exist_ok=True)
    novo = not LOG_CSV.exists() or LOG_CSV.stat().st_size == 0
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS_LOG, extrasaction="ignore")
        if novo:
            w.writeheader()
        w.writerow({c: linha.get(c, "") for c in COLUNAS_LOG})
    log(f"log registrado: {LOG_CSV.relative_to(ROOT)} (status={linha.get('status')})")


def main():
    st = {"status": "erro"}
    try:
        executar(st)
    except SystemExit as e:                     # sys.exit("mensagem") = falha esperada
        if e.code not in (None, 0):
            st.update(status="erro", mensagem=str(e.code)[:300]); registrar_log(st)
        raise
    except Exception as e:                      # noqa: BLE001
        st.update(status="erro", mensagem=f"{type(e).__name__}: {e}"[:300]); registrar_log(st)
        raise
    else:
        registrar_log(st)


def executar(st: dict):
    ap = argparse.ArgumentParser(); ap.add_argument("--file"); ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    zp = obter_zip(args)
    digest = hashlib.sha256(zp.read_bytes()).hexdigest() if zp.stat().st_size < (1 << 31) else "grande"
    st.update(origem_url=os.environ.get("DATA_URL") or str(args.file), sha256_zip=digest, tamanho_zip_mb=round(zp.stat().st_size / 2**20, 1))
    meta_p = DATA / "meta.json"
    ant = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    if not args.force and ant.get("sha256") == digest and ant.get("versao") == VERSAO:
        log("arquivo inalterado desde a última execução; nada a reprocessar.")
        st.update(status="sem_novidades", mensagem="Arquivo da Anatel inalterado (mesmo sha256); nada a reprocessar.",
                  registros_origem=ant.get("registros_origem", ""), linhas_agregadas=ant.get("linhas_agregadas", ""),
                  periodo_inicio=(ant.get("periodo") or ["", ""])[0], periodo_fim=(ant.get("periodo") or ["", ""])[1],
                  ultima_data_abertura=ant.get("ultima_data_abertura", ""))
        marcar_verificacao(False); return
    motivo = ("Arquivo novo na fonte (sha256 diferente)." if ant.get("sha256") != digest else
              f"Reprocessado: versão do ETL mudou ({ant.get('versao')} → {VERSAO})." if ant.get("versao") != VERSAO else "Reprocessamento forçado (--force).")

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
    n3 = gravar("por_dia", ["dia", "servico", "prestadora", "dem", "resp", "resol", "aval"], con.execute(AGG_DIA).fetchall())
    for r in con.execute("SELECT dia, count(*), sum(resp::INT), sum(resol::INT), sum(aval::INT) FROM base "
                         "WHERE regexp_matches(dia, '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') GROUP BY dia ORDER BY dia DESC LIMIT 5").fetchall():
        log(f"  dia {r[0]}: total={r[1]} respondidas={r[2]} resolvidas(S)={r[3]} avaliadas={r[4]}")
    mn, mx = con.execute("SELECT min(mes), max(mes) FROM base").fetchone()
    ultima = con.execute("SELECT max(dia) FROM base WHERE regexp_matches(dia, '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')").fetchone()[0] or ""
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta_p.write_text(json.dumps({
        "atualizado_em": agora, "ultima_data_abertura": ultima,
        "origem": os.environ.get("DATA_URL", str(args.file)), "sha256": digest,
        "registros_origem": n, "periodo": [mn, mx], "linhas_agregadas": n1 + n2 + n3, "versao": VERSAO, "medidas": MEDIDAS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    marcar_verificacao(True)
    st.update(status="atualizado", data_hora_atualizacao_utc=agora, registros_origem=n, linhas_agregadas=n1 + n2 + n3,
              periodo_inicio=mn, periodo_fim=mx, ultima_data_abertura=ultima, mensagem=motivo)
    log(f"concluído ({mn} → {mx})")


if __name__ == "__main__":
    main()
