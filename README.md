# 123 Milhas — Dashboard App x Web

Dashboard estático (HTML + Chart.js via CDN) comparando vendas via **app**
(iOS/Android) x **web** (Mobile/DeskTop) da 123 Milhas, com base em
`grupo123-metrics.df_granular.orders` (BigQuery).

Sem backend: `index.html` lê `data.json` (commitado no repo) direto no browser.
Deploy simples via Vercel (import do repo, sem build step / sem configuração).

## Estrutura

- `index.html` — dashboard com 2 abas: "Visão geral" (stat tiles, série
  diária, origem das vendas) e "Intraday" (hoje x ontem x mesmo dia da semana
  passada, acumulado por hora, em tempo real)
- `data.json` — série diária por plataforma (ERP) + bloco de origem
- `intraday.json` — por hora de hoje/ontem/semana passada + origem do dia
- `query.sql` — query de referência usada para gerar a série diária
- `refresh_data.py` — gera os dois arquivos a partir do BigQuery

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

`--only intraday` roda só a parte barata (a aba Intraday), e é o que roda de
10 em 10 minutos no cron. Sem `--only`, roda tudo: recarrega a origem, o
`data.json` e o `intraday.json`.

**Cron** (droplet `loop-hefesto-atlas`, usuário `loop`):

- `*/10 * * * *` → `--only intraday` (tempo real)
- `7 * * * *` → rodada completa (série diária + origem)

**Definição de "dia":** ambos os arquivos usam o campo `order_date` da
tabela (não `DATE(created_at, "America/Sao_Paulo")` puro) — os dois
divergem perto da virada UTC porque `order_date` parece ser atribuído em
UTC. Manter os dois arquivos na mesma definição evita números que não batem
entre as duas abas.

**Atraso de ingestão do ERP (medido, não é bug do dash):** `df_granular.orders`
recebe uma carga a cada 20 minutos, mas cada carga traz pedidos de ~3h atrás.
Em 10/08/2026 a carga também parou por 3h (última às 07:40, com o pedido mais
novo criado 04:35 — ou seja, dado 6h velho). É por isso que a aba Intraday
**não** usa mais o ERP como linha principal: ela vem do evento `purchase` do
GA4, que é tempo real. O número do ERP aparece no rodapé só como conferência.

**Dois recortes automáticos na série diária:** o `refresh_data.py` corta os
dias iniciais com menos de 500 pedidos (a tabela do ERP só ganha volume real a
partir de 30/06/2026; antes disso são semanas de linha rente ao zero) e tira o
dia corrente, que sempre chega pela metade por causa do atraso acima.

Query de referência pra série diária (equivalente ao que `refresh_data.py`
roda): ver `query.sql`.

## Origem das vendas

O ERP não carrega utm/origem até o pedido, e o `mm_tracker` do evento de
checkout não resolve: ele existe em 96% dos pedidos web mas em **0%** dos
pedidos de app, e só 22% dos `client_id` dele existem no GA4. O caminho que
funciona é o próprio evento `purchase` do GA4, cujo `transaction_id` é
`"f-" + order_id` do ERP:

- casa **~84%** dos pedidos do ERP, igual pra app (84,3%) e web (84,8%)
- desses, 99,4% têm sessão com origem (`session_traffic_source_last_click`)

O export do GA4 (`analytics_327722742`) mora em **US** e o ERP em
**southamerica-east1**, e o BigQuery não faz JOIN entre regiões. Em vez de
montar uma ponte cross-region (custo contínuo), a origem é materializada em
`grupo123-metrics.df_granular_us.order_origin` (tabela pequena, ~4 mil
linhas/dia, particionada por `order_date`) e o cruzamento com o ERP acontece
no Python, por agregado.

Essa tabela **só acumula, nunca é truncada**: o export do GA4 é apagado depois
de ~36 dias, então ela é o único registro histórico de origem que sobra.

O bloco de origem no dashboard não mostra o volume do GA4: mostra o volume do
**ERP**, rateado pela participação de cada canal medida no GA4 naquele mesmo
dia e plataforma. Assim ele fecha exatamente com os totais do topo. Dia sem
base suficiente no GA4 (menos de 20 pedidos) aparece como "Sem origem" em vez
de ser rateado em cima de ruído.

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
