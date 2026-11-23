"""ETL: baixa o ZIP de "Consumidor - Indicadores de Qualidade de Atendimento" (Anatel),
normaliza e grava data/registros.json + data/meta.json.

Uso:
    DATA_URL="https://.../arquivo.zip" python scripts/etl.py
    python scripts/etl.py --file caminho/local.zip|.csv
    python scripts/etl.py --sample          # dados sintéticos (apenas demonstração)

Como o esquema exato do arquivo pode mudar, as colunas são detectadas por
apelidos (ALIASES). Se o arquivo for "largo" (várias colunas numéricas, sem
coluna indicador/valor), as colunas numéricas viram indicadores.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

ALIASES = {
    "periodo": ["periodo", "mes_ano", "ano_mes", "competencia", "data", "dt_referencia",
                "data_referencia", "mes_referencia"],
    "ano": ["ano", "ano_referencia"],
    "mes": ["mes", "mes_referencia_num"],
    "prestadora": ["prestadora", "empresa", "grupo_economico", "operadora",
                   "nome_prestadora", "nome_empresa", "grupo"],
    "servico": ["servico", "tipo_servico", "produto", "servico_prestado"],
    "indicador": ["indicador", "nome_indicador", "tipo_indicador"],
    "valor": ["valor", "resultado", "indice", "percentual", "taxa", "quantidade", "total"],
}
DIMS = ["periodo", "prestadora", "servico", "indicador"]


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def read_bytes(args) -> tuple[bytes, str]:
    if args.file:
        p = Path(args.file)
        return p.read_bytes(), p.name
    url = os.environ.get("DATA_URL")
    if not url:
        sys.exit("Defina DATA_URL (link do ZIP/CSV no dados.gov.br/Anatel) ou use --file/--sample.")
    import time
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "*/*"})
    inicio, buf, limite = time.time(), bytearray(), 600  # no máx. 10 min
    print(f"Baixando {url}", flush=True)
    with urllib.request.urlopen(req, timeout=60) as r:
        print(f"HTTP {r.status} · tamanho: {r.headers.get('Content-Length', '?')} bytes", flush=True)
        while chunk := r.read(1 << 20):
            buf += chunk
            if time.time() - inicio > limite:
                sys.exit("Download excedeu o tempo limite.")
            if len(buf) % (20 << 20) < (1 << 20):
                print(f"  {len(buf) >> 20} MB…", flush=True)
    print(f"Download ok: {len(buf) >> 20} MB em {time.time() - inicio:.0f}s", flush=True)
    return bytes(buf), url


def read_tables(raw: bytes, name: str) -> list[pd.DataFrame]:
    blobs = []
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for n in z.namelist():
                if n.lower().endswith((".csv", ".txt")):
                    blobs.append(z.read(n))
    else:
        blobs.append(raw)
    if not blobs:
        sys.exit("Nenhum CSV encontrado no arquivo.")
    out = []
    for b in blobs:
        for enc in ("utf-8-sig", "latin-1"):
            try:
                text = b.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        sep = ";" if text[:2000].count(";") >= text[:2000].count(",") else ","
        out.append(pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False))
    return out


def to_num(s: pd.Series) -> pd.Series:
    t = s.astype(str).str.replace("%", "", regex=False).str.strip()
    # formato BR: 1.234,56  ->  1234.56
    br = t.str.contains(",", regex=False)
    t = t.where(~br, t.str.replace(".", "", regex=False).str.replace(",", ".", regex=False))
    return pd.to_numeric(t, errors="coerce")


def pick(cols: list[str], key: str) -> str | None:
    for a in ALIASES[key]:
        if a in cols:
            return a
    return None


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [slug(c) for c in df.columns]
    cols = list(df.columns)

    # período
    c = pick(cols, "periodo")
    if c:
        dt = pd.to_datetime(df[c], errors="coerce", dayfirst=True)
        if dt.isna().mean() > 0.5:  # tenta "2024-05" / "05/2024"
            dt = pd.to_datetime(df[c].str.replace(r"^(\d{2})/(\d{4})$", r"\2-\1", regex=True),
                                errors="coerce")
        df["periodo"] = dt.dt.strftime("%Y-%m")
    elif pick(cols, "ano") and pick(cols, "mes"):
        a, m = pick(cols, "ano"), pick(cols, "mes")
        df["periodo"] = (pd.to_numeric(df[a], errors="coerce").astype("Int64").astype(str) + "-"
                         + pd.to_numeric(df[m], errors="coerce").astype("Int64").astype(str).str.zfill(2))
    else:
        df["periodo"] = "s/d"

    for k in ("prestadora", "servico"):
        c = pick(cols, k)
        df[k] = df[c].str.strip() if c else "Todas"

    ind, val = pick(cols, "indicador"), pick(cols, "valor")
    if ind and val:
        df["indicador"] = df[ind].str.strip()
        df["valor"] = to_num(df[val])
    else:  # formato largo -> derrete colunas numéricas
        used = {"periodo", "prestadora", "servico", "ano", "mes"} | {
            pick(cols, k) for k in ("periodo", "ano", "mes", "prestadora", "servico")}
        cand = [c for c in cols if c not in used and c not in DIMS]
        nums = [c for c in cand if to_num(df[c]).notna().mean() > 0.8]
        if not nums:
            sys.exit(f"Não consegui identificar colunas de valor. Colunas: {cols}")
        long = df.melt(id_vars=["periodo", "prestadora", "servico"], value_vars=nums,
                       var_name="indicador", value_name="valor")
        long["valor"] = to_num(long["valor"])
        df = long
    df = df.dropna(subset=["valor"])
    return df[DIMS + ["valor"]]


def sample() -> pd.DataFrame:
    import random
    random.seed(7)
    rows = []
    for ano in (2024, 2025, 2026):
        for mes in range(1, 13):
            if ano == 2026 and mes > 10:
                break
            for p in ("Operadora A", "Operadora B", "Operadora C", "Operadora D"):
                for s in ("Telefonia móvel", "Banda larga fixa"):
                    for ind, lo, hi in (("Reclamações respondidas no prazo (%)", 70, 98),
                                        ("Reclamações resolvidas (%)", 55, 92),
                                        ("Nota média do atendimento", 2.5, 4.6)):
                        rows.append((f"{ano}-{mes:02d}", p, s, ind, round(random.uniform(lo, hi), 2)))
    return pd.DataFrame(rows, columns=DIMS + ["valor"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--sample", action="store_true")
    args = ap.parse_args()

    if args.sample:
        df, origem, is_sample, digest = sample(), "dados sintéticos (demonstração)", True, "sample"
    else:
        raw, origem = read_bytes(args)
        digest = hashlib.sha256(raw).hexdigest()
        df = pd.concat([normalize(t) for t in read_tables(raw, origem)], ignore_index=True)
        is_sample = False

    df = df.groupby(DIMS, as_index=False)["valor"].mean().round(4)
    df = df.sort_values(DIMS)

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "registros.json").write_text(
        json.dumps(df.values.tolist(), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    meta = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origem": origem if is_sample else os.environ.get("DATA_URL", origem),
        "amostra": is_sample,
        "linhas": int(len(df)),
        "sha256": digest,
        "colunas": DIMS + ["valor"],
    }
    (DATA_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {len(df)} linhas -> data/registros.json ({'AMOSTRA' if is_sample else 'dados reais'})")


if __name__ == "__main__":
    main()
