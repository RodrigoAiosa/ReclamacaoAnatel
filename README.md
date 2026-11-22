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
1. No GitHub: **Settings → Secrets and variables → Actions → Variables** → crie `DATA_URL` com o link direto do
   arquivo ZIP/CSV do recurso "Indicadores de Qualidade de Atendimento" (botão *Baixar* no dados.gov.br).
2. Rode o workflow **Atualizar dados** (Actions → *Run workflow*) para popular `data/`.
3. Na Vercel: **Add New → Project** → importe este repositório (sem build command; output `public`).

## Rodar localmente
```bash
pip install -r requirements-etl.txt
python scripts/etl.py --sample                 # dados sintéticos
DATA_URL="https://..." python scripts/etl.py   # dados reais
npx vercel dev                                  # http://localhost:3000
```

## Sobre o ETL
O esquema do arquivo é detectado por apelidos de colunas (`ALIASES` em `scripts/etl.py`): período
(ou ano+mês), prestadora, serviço, indicador, valor. Arquivos "largos" (uma coluna por indicador) são
convertidos automaticamente. Se o layout real divergir, ajuste `ALIASES`.

> Os arquivos em `data/` do primeiro commit são **sintéticos** (o dashboard exibe um aviso).
> Serão substituídos pelos dados reais na primeira execução do workflow com `DATA_URL`.
