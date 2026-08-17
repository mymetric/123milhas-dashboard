#!/usr/bin/env bash
# Atualiza os snapshots do dashboard 123 Milhas e sobe pra main (deploy Vercel).
#
# Dois modos, porque as duas metades tem custo bem diferente no BigQuery:
#   intraday  -> so a aba Intraday, do evento purchase do GA4 em tempo real.
#                Roda de 10 em 10 min (~0,5 GB por rodada).
#   (vazio)   -> rodada completa: le a origem de order_origin_full (mantida pelo Dataform), a serie
#                diaria do ERP e o intraday. Roda de hora em hora.
set -uo pipefail

REPO="/home/loop/loop/123milhas-dashboard"
SA_KEY="/home/loop/loop/sa_123.json"
MODO="${1:-all}"

cd "$REPO" || exit 1

# o cron so escreve nesses tres arquivos; qualquer outra mudanca vem do git
git fetch -q origin main && git reset -q --hard origin/main

/home/loop/loop/venv/bin/python3 refresh_data.py --sa-key "$SA_KEY" --only "$MODO" >>/tmp/123milhas_refresh.log 2>&1
RC=$?
if [ $RC -ne 0 ]; then
  echo "$(date -Is) refresh falhou (modo=$MODO, rc=$RC)" >>/tmp/123milhas_refresh.log
  exit $RC
fi

if ! git diff --quiet -- data.json intraday.json pedidos.csv; then
  git add data.json intraday.json pedidos.csv
  git commit -m "Atualiza snapshot de dados ($MODO)" -q
  git push -q origin main >>/tmp/123milhas_refresh.log 2>&1
fi
