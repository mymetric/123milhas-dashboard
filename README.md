# 123 Milhas — Dashboard App x Web

Dashboard estático (HTML + Chart.js via CDN) comparando vendas via **app**
(iOS/Android) x **web** (Mobile/DeskTop) da 123 Milhas, com base em
`grupo123-metrics.df_granular.orders` (BigQuery).

Sem backend: `index.html` lê `data.json` (commitado no repo) direto no browser.
Deploy simples via Vercel (import do repo, sem build step / sem configuração).

## Estrutura

- `index.html` — dashboard com 2 abas: "Visão geral" (stat tiles + 2 gráficos
  de série temporal, 90 dias) e "Intraday" (hoje x ontem x mesmo dia da
  semana passada, acumulado por hora)
- `data.json` — dados agregados por dia/plataforma, últimos 90 dias
- `intraday.json` — dados por hora de hoje/ontem/semana passada (acumulado)
- `query.sql` — query de referência usada para gerar a série diária
- `refresh_data.py` — gera `data.json` e `intraday.json` a partir do BigQuery

## Como atualizar `data.json` e `intraday.json`

O projeto GCP `grupo123-metrics` é separado do hub principal. A chave de
serviço usada está em `sa_123.json` (não versionada aqui por segurança — pegue
com o time de dados/infra).

```bash
python3 refresh_data.py --sa-key /caminho/sa_123.json
git add data.json intraday.json
git commit -m "Atualiza dados"
git push origin main   # deploy automático na Vercel
```

Sem `--sa-key`, o script tenta `$SA_123_KEY` ou `../sa_123.json`.

**`intraday.json` é um snapshot, não live** — só atualiza quando o script
roda de novo. Não há cron configurado ainda; se quiser a aba Intraday sempre
fresca ao longo do dia, precisa agendar esse script (ex. GitHub Actions a
cada N minutos) — melhoria futura em aberto, junto com a mesma automação
pendente pra `data.json` (ver histórico abaixo).

**Definição de "dia":** ambos os arquivos usam o campo `order_date` da
tabela (não `DATE(created_at, "America/Sao_Paulo")` puro) — os dois
divergem perto da virada UTC porque `order_date` parece ser atribuído em
UTC. Manter os dois arquivos na mesma definição evita números que não batem
entre as duas abas.

**Atenção a atraso de ingestão:** o dado mais recente da fonte
(`MAX(created_at)`) pode ficar horas atrás do relógio atual (já visto ~12h de
atraso) — o `refresh_data.py` capa a série de "hoje" na hora do dado mais
recente de fato (não no horário de agora), e o rodapé da aba Intraday mostra
esse timestamp. Se aparecer muito atrasado, o pipeline de ingestão upstream
pode estar com problema — vale checar antes de assumir queda real de vendas.

Query de referência pra série diária (equivalente ao que `refresh_data.py`
roda): ver `query.sql`.

## Definição de "app" x "web"

Campo `device` na tabela `grupo123-metrics.df_granular.orders`:

- **app**: `iOS`, `Android`
- **web**: `Mobile`, `DeskTop`

(Existem raríssimos registros com `device` nulo — não entram em nenhuma das
duas séries.)

## Deploy na Vercel

Este repo é 100% estático (sem `package.json`, sem build). Basta importar o
repo `mymetric/123milhas-dashboard` na conta Vercel e configurar "Framework
Preset: Other" / sem build command — a Vercel serve o `index.html` como está.
Depois do import inicial, qualquer push na `main` já deploya sozinho.
