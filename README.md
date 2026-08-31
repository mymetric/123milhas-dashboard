# 123 Milhas — Dashboard App x Web

Dashboard estático (HTML + Chart.js via CDN) comparando vendas via **app**
(iOS/Android) x **web** (Mobile/DeskTop) da 123 Milhas, com base em
`grupo123-metrics.df_granular.orders` (BigQuery).

Sem backend: `index.html` lê `data.json` (commitado no repo) direto no browser.
Deploy simples via Vercel (import do repo, sem build step / sem configuração).

## Estrutura

- `index.html` — dashboard com 3 abas: "Visão geral" (stat tiles, composição do
  faturamento, série diária, origem das vendas), "Intraday" (hoje x ontem x
  mesmo dia da semana passada, acumulado por hora, em tempo real) e "Pedidos"
  (uma linha por pedido, para export)
- `data.json` — série diária por plataforma (ERP) + bloco de origem
- `intraday.json` — por hora de hoje/ontem/semana passada + origem do dia
- `pedidos.csv` — 1 linha por pedido, 30 dias, com valor decomposto e atribuição
- `categorias.json` — cadastro de categorias de tráfego (origem × mídia)
- `cron/` — cópia versionada do script que roda no droplet
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

**O ERP não é intraday (medido, não é bug do dash):** `df_granular.orders` é
reconstruída **uma vez por dia, às 08:00**, por um `CREATE OR REPLACE TABLE` do
Dataform (confirmado em `INFORMATION_SCHEMA.JOBS`: exatamente 4 jobs às 08:00,
todo dia, sem exceção nos últimos 7 dias). Durante o resto do dia a tabela não
muda — não existe carga intraday pra estar atrasada ou pra "cair".

Dentro de cada build, o `received_at` que vem no dado mostra a esteira upstream
rodando de 20 em 20 minutos, e cada lote carrega pedidos de ~3h antes. Por isso
o build das 08:00 chega com o pedido mais novo por volta das 04:30 — é esse o
"trava às 4-5h da manhã" que aparecia na aba Intraday antiga.

É por isso que a aba Intraday **não** usa mais o ERP como linha principal: ela
vem do evento `purchase` do GA4, que é tempo real. O número do ERP aparece no
rodapé só como conferência do que já fechou.

Se quiserem número do ERP ao longo do dia, o caminho é aumentar a frequência do
schedule do Dataform — não tem o que ajustar no dashboard.

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

Como o dashboard reclassifica a origem por conta própria (ver "Categorias de
tráfego"), o `intraday.json` carrega `source`/`medium` crus junto com o canal —
sem eles a aba Intraday ficaria presa na taxonomia do BigQuery.

O bloco de origem no dashboard não mostra o volume do GA4: mostra o volume do
**ERP**, rateado pela participação de cada canal medida no GA4 naquele mesmo
dia e plataforma. Assim ele fecha exatamente com os totais do topo. Dia sem
base suficiente no GA4 (menos de 20 pedidos) aparece como "Sem origem" em vez
de ser rateado em cima de ruído.


## Categorias de tráfego (`categorias.json`)

O `canal` que vem do BigQuery é só o **padrão de fábrica**. Quem manda no
dashboard é o `categorias.json`: uma lista **ordenada** de categorias, cada uma
com regras que combinam **origem** (`source`) e **mídia** (`medium`) da sessão.

```json
{ "nome": "Mídia paga", "cor": "#eb6834",
  "regras": [ { "medium": { "op": "in", "valor": "cpc, ppc, cpa, paid" } } ] }
```

- uma regra pode ter as duas condições (valem juntas, é um E) ou só uma;
- vale a **primeira** categoria de cima para baixo com alguma regra que casa —
  por isso a ordem importa, e o botão ▲ do editor é o que resolve conflito;
- operadores: `qualquer`, `eq`, `in` (lista separada por vírgula), `contem`,
  `comeca`, `regex`. Tudo sem diferenciar maiúscula de minúscula;
- o que não casa com ninguém cai em **Outros**;
- **Sem origem** fica fora das regras de propósito: é o pedido que nenhuma rota
  de atribuição resolveu, e é ele que faz o bloco fechar com o total do ERP.
  Uma regra larga engolindo ele quebraria essa conta.

O cadastro vale para o bloco "Origem das vendas" (ranking, tabela origem/mídia,
timelines com filtro ligado), para a aba **Pedidos** (as colunas de categoria
saem de origem/mídia, não da coluna `canal_*` do CSV) e para a aba **Intraday**.

**Editando pelo dashboard:** botão `categorias` no bloco "Origem das vendas".
Cada regra mostra quantos pedidos ela captura no recorte aberto, e a lista
"Não classificados" embaixo é a fila de trabalho — todo par de origem/mídia que
ainda cai em "Outros", do maior para o menor, com um clique para classificar.

**Onde a edição fica salva:** no `localStorage` do navegador de quem editou.
Para virar o padrão de todo mundo, use **exportar JSON** e commite o arquivo por
cima de `categorias.json`. Enquanto o navegador tiver cadastro próprio, o rodapé
do editor avisa; **restaurar padrão** + salvar apaga a cópia local e volta a
seguir o arquivo do repo.

Os defaults do arquivo reproduzem exatamente a taxonomia que o BigQuery já
aplicava — instalar isso não mexeu em nenhum número.

## Como o faturamento é calculado (e por que não é o `grand_total`)

```
faturamento = sub_total + (tax + discount) − desconto
desconto    = sub_total + tax + discount − grand_total   (0 se grand_total for NULL)
```

Dois motivos para não somar `grand_total` direto:

**1. O campo `discount` da tabela é acréscimo, não desconto.** Medido em 60
dias / 168 mil pedidos com `grand_total` preenchido:

- 123.364 (73%): `grand_total = sub_total + tax + discount`, exato
- 44.409 (26%): `discount = 0` e o `grand_total` fica **abaixo** de
  `sub_total + tax` — R$ 6,28 mi de desconto que não está em coluna nenhuma
- **zero** pedidos em que `grand_total = sub_total + tax − discount`

Ou seja: a 123 não expõe o desconto, ela expõe o resultado. O desconto real só
existe como sobra, e é assim que o `refresh_data.py` o deriva. O painel
"Composição do faturamento" mostra as três parcelas separadas.

**2. `grand_total` é NULL em 98.770 pedidos** de 30/06 a 06/07/2026 (o pico de
promoção, `last_status = 5`), com `sub_total`, `tax` e `discount` preenchidos
normalmente. Somando `grand_total`, esses dias apareciam com 19-32 mil pedidos e
ticket médio de R$ 190 — R$ 219 milhões de faturamento sumindo do gráfico.
Partindo do `sub_total`, eles voltam.

Onde o `grand_total` existe a fórmula o reproduz exatamente (ela é a inversa
dele), então nada muda de 07/07 em diante. Onde ele é NULL, o desconto entra
como 0 e sobra `sub_total + taxas` — a melhor estimativa disponível, e o
dashboard avisa em nota quais dias do recorte estão nessa situação.

O bloco de origem continua vindo do `grand_total` (é o que a tabela
`order_origin_full` replica), mas cada célula dia × plataforma é reescalada
para o total do topo, então os dois seguem fechando.

## Aba "Pedidos" (export)

`pedidos.csv` tem uma linha por pedido dos últimos 30 dias (sem o dia corrente,
mesma regra da série diária), com o valor decomposto — subtotal, taxas,
descontos, faturamento — e a atribuição de **último** e de **primeiro clique**
lado a lado, cada uma com canal, origem, mídia e campanha, mais o `metodo_match`
(qual rota casou o pedido com a sessão do GA4).

O modelo **data-driven do GA4 não entra**: ele não existe por pedido. O crédito
vem fracionado da Data API e só agregado por dia × plataforma × origem/mídia.

Sai de `df_granular_us.order_origin_full` numa query só — a tabela já é uma
linha por `order_id` e, desde 17/08/2026, carrega também `sub_total`, `tax`,
`discount` e `receita`, replicados do ERP via `df_granular.orders_keys`.

São ~110 mil linhas / ~17 MB (2 MB comprimido, que é como a Vercel serve). O
`index.html` só baixa o arquivo quando a aba é aberta; a tabela mostra os 200
pedidos mais recentes do recorte e o botão "baixar CSV" leva o recorte inteiro.

O arquivo é regenerado na rodada completa (de hora em hora), mas fica **byte a
byte igual dentro do mesmo dia** — o ERP só é reconstruído 1x/dia e o dia
corrente fica de fora. Na prática o cron só gera commit dele uma vez por dia.

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
