# ReclamacaoAnatel

Dashboard dos **Indicadores de Qualidade de Atendimento** do Anatel Consumidor (reclamações de telecom),
com dados abertos de [dados.gov.br](https://dados.gov.br/dados/conjuntos-dados/consumidor---indicadores-de-qualidade-de-atendimento).
Python + Vercel.

## Arquitetura
```
GitHub Actions (cron, a cada 2 dias)
  └─ scripts/etl.py  → baixa o ZIP (DATA_URL), normaliza → data/registros.json + data/meta.json
        └─ commit → Vercel faz novo deploy
Vercel
  ├─ api/dados.py    → função Python: filtros, séries, ranking, KPIs  (GET /api/dados)
  └─ public/index.html → dashboard (Chart.js)
```

## Configuração (1x)
1. (Opcional) crie a variável `DATA_URL` em Settings → Secrets and variables → Actions → Variables; sem ela o
   workflow usa o link oficial da Anatel já definido em `atualizar-dados.yml`.
2. O workflow **Atualizar dados** roda a cada 2 dias e também ao alterar `scripts/etl.py` (ou manualmente).
3. Na Vercel: **Add New → Project** → importe este repositório (sem build command; output `public`).

## Rodar localmente
```bash
pip install -r requirements-etl.txt
DATA_URL="https://www.anatel.gov.br/dadosabertos/paineis_de_dados/consumidor/indicadores_qualidade_atendimento.zip" python scripts/etl.py
npx vercel dev                                  # http://localhost:3000
```

## Sobre os dados e o ETL
O arquivo da Anatel (`AnatelConsumidorReclamacoesCSV.csv`, ~2,4 GB, uma linha por demanda) é agregado com
**DuckDB** em ~30 s por `scripts/etl.py`, gerando `data/por_uf.json.gz` e `data/por_assunto.json.gz`
(mês × serviço × prestadora × UF/assunto, com somas e contagens). A API calcula os indicadores a partir delas:

| Métrica | Definição |
|---|---|
| Reclamações | demandas com `TIPO_ATENDIMENTO = Reclamação` |
| % respondidas | `RESPONDIDA = S` ÷ demandas |
| % resolvidas | `RESOLVIDA = S` ÷ demandas avaliadas (`S` ou `N`) |
| % reabertas | `REABERTA = S` ÷ demandas |
| Nota média | média de `NOTA_CONSUMIDOR` (1–5) |
| Prazo médio | média de `PrazoResposta` nas respondidas |

Se o ZIP não mudou (sha256), o ETL sai sem reprocessar.
