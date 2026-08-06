# 123 Milhas — Dashboard App x Web

Dashboard estático (HTML + Chart.js via CDN) comparando vendas via **app**
(iOS/Android) x **web** (Mobile/DeskTop) da 123 Milhas, com base em
`grupo123-metrics.df_granular.orders` (BigQuery).

Sem backend: `index.html` lê `data.json` (commitado no repo) direto no browser.
Deploy simples via Vercel (import do repo, sem build step / sem configuração).

## Estrutura

- `index.html` — dashboard (stat tiles + 2 gráficos de série temporal: pedidos e faturamento)
- `data.json` — dados agregados por dia/plataforma, gerados a partir do BigQuery
- `query.sql` — query usada para gerar `data.json`

## Como atualizar `data.json`

O projeto GCP `grupo123-metrics` é separado do hub principal. A chave de
serviço usada está em `sa_123.json` (não versionada aqui por segurança — pegue
com o time de dados/infra).

1. Autentique com a service account do projeto `grupo123-metrics`:

   ```bash
   gcloud auth activate-service-account --key-file=/caminho/sa_123.json \
     --account=mymetric@grupo123-metrics.iam.gserviceaccount.com
   TOKEN=$(gcloud auth print-access-token --account=mymetric@grupo123-metrics.iam.gserviceaccount.com)
   ```

   Nota: o `bq` CLI padrão pode ignorar `GOOGLE_APPLICATION_CREDENTIALS` em
   ambientes com `CLOUDSDK_CORE_ACCOUNT` fixo para outra conta. Se isso
   acontecer, use a API REST do BigQuery diretamente (como abaixo).

2. Rode a query (`query.sql`) via API REST:

   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     https://bigquery.googleapis.com/bigquery/v2/projects/grupo123-metrics/queries \
     -d "$(python3 -c "import json; print(json.dumps({'query': open('query.sql').read(), 'useLegacySql': False, 'maxResults': 1000}))")" \
     > result.json
   ```

3. Transforme o resultado no formato de `data.json` (data, plataforma,
   pedidos, faturamento → série diária + resumo do período). Não há script de
   transformação automatizado ainda — isso é uma melhoria futura em aberto
   (pipeline agendado, ex. GitHub Actions rodando diariamente).

4. Commit + push do novo `data.json` na `main`. Push na main já dispara deploy
   automático na Vercel (depois que o repo for importado lá).

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
