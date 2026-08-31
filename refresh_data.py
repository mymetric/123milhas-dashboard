#!/usr/bin/env python3
"""Gera os snapshots do dashboard 123 Milhas.

  data.json      serie diaria (90 dias) de pedidos/faturamento app x web, do ERP,
                 + bloco de origem de sessao (GA4 last click)
  intraday.json  hoje x ontem x mesmo dia da semana passada, por hora, em tempo
                 real (GA4), com a origem do dia e o numero oficial do ERP
  pedidos.csv    1 linha por pedido (30 dias), com o valor decomposto e a
                 atribuicao de ultimo e de primeiro clique -- a aba "Pedidos"

Fontes e por que sao duas:
  - `grupo123-metrics.df_granular.orders` (southamerica-east1) e a verdade de
    faturamento, mas chega com ~3h de atraso de ingestao.
  - export do GA4 (`analytics_327722742`, US) tem o evento `purchase` em tempo
    real (tabela events_intraday_*) e a origem de sessao ja resolvida. Casa com o
    ERP por `transaction_id = "f-" + order_id` em ~85% dos pedidos, igual pra app
    e web.
  O BigQuery nao faz JOIN entre US e southamerica-east1, entao o cruzamento
  acontece aqui em Python, por order_id / por agregado.

Uso:
  python3 refresh_data.py --sa-key /path/sa_123.json            # tudo
  python3 refresh_data.py --sa-key ... --only intraday          # so o intraday
  python3 refresh_data.py --sa-key ... --only pedidos           # so o CSV de pedidos

Sem --sa-key, tenta $SA_123_KEY ou ../sa_123.json.
"""
import argparse
import datetime
import json
import os
import sys

from google.oauth2 import service_account
import google.auth.transport.requests
import requests

PROJECT = "grupo123-metrics"
# so leitura: quem mantem a tabela de origem agora e o Dataform (repo
# ga4-sessions, projeto grupo123-metrics), nao este script.
SCOPES = ["https://www.googleapis.com/auth/bigquery"]
BRT = datetime.timezone(datetime.timedelta(hours=-3))

GA4_DATASET = "grupo123-metrics.analytics_327722742"
ORIGIN_TABLE = "grupo123-metrics.df_granular_us.order_origin_full"
# credito do modelo data-driven do proprio GA4 (Data API -> tools/ga4_dda_to_bq.py)
DDA_TABLE = "grupo123-metrics.df_granular_us.ga4_dda_canal"
ERP_TABLE = "grupo123-metrics.df_granular.orders"

# --------------------------------------------------------------------------
# Composicao do faturamento
#
# O ERP nao tem uma coluna de desconto utilizavel: o campo `discount` da tabela
# e ACRESCIMO. Medido em 60 dias / 168 mil pedidos com grand_total preenchido:
#   - 123.364 (73%): grand_total = sub_total + tax + discount, exato
#   -  44.409 (26%): discount = 0 e o grand_total fica ABAIXO de sub_total+tax
#                    (R$ 6,28 mi de desconto que nao esta em coluna nenhuma)
#   -       0      : nenhum pedido em que grand_total = sub_total + tax - discount
# Logo o desconto real e implicito, e sai da sobra:
#   desconto = sub_total + tax + discount - grand_total
#
# E o `grand_total` sozinho NAO serve de faturamento: 98.770 pedidos de
# 30/06 a 06/07/2026 (o pico de promocao, last_status=5) tem grand_total NULL
# com sub_total/tax/discount preenchidos. Somar grand_total joga fora ~R$ 219 mi
# e desenha aqueles dias como 19-32 mil pedidos com ticket de R$ 190. Por isso o
# faturamento do dashboard e montado a partir do sub_total:
#
#   faturamento = sub_total + (tax + discount) - desconto
#
# Onde o grand_total existe isso reproduz o grand_total exatamente (a formula e
# a inversa dele); onde ele e NULL, o desconto entra como 0 e sobra
# sub_total + taxas, que e a melhor estimativa disponivel.
RECEITA_SQL = """
      SUM(sub_total) AS subtotal,
      SUM(tax + discount) AS taxas,
      SUM(IF(grand_total IS NULL, 0, sub_total + tax + discount - grand_total)) AS descontos,
      SUM(grand_total) AS grand_total
"""


def _receita(row):
    """Componentes de faturamento de uma linha do RECEITA_SQL -> dict de floats."""
    f = lambda c: round(float(row[c] or 0), 2)
    sub, tax, desc = f("subtotal"), f("taxas"), f("descontos")
    return {"subtotal": sub, "taxas": tax, "descontos": desc,
            "revenue": round(sub + tax - desc, 2),
            # so como referencia do bloco de origem, que ainda vem do grand_total
            "revenue_grand": f("grand_total")}


# Quantos dias reprocessar de origem a cada rodada. O GA4 fecha a tabela do dia
# so na madrugada seguinte, e ainda corrige numeros por ~2 dias.


def bq_query(creds, sql, params=None):
    """Roda uma query (ou DML) e devolve as linhas ja como dicts.

    Pagina ate o fim: algumas queries passam do teto de linhas de uma resposta so.
    """
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
    body = {"query": sql, "useLegacySql": False, "maxResults": 20000, "timeoutMs": 120000}
    if params:
        body["queryParameters"] = params
        body["parameterMode"] = "NAMED"

    r = requests.post(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries",
        headers=headers, json=body, timeout=180,
    )
    r.raise_for_status()
    data = r.json()

    job = data.get("jobReference", {})
    # DML e queries longas voltam sem jobComplete; busca o resultado pelo job id
    while not data.get("jobComplete", True):
        r = requests.get(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries/{job['jobId']}",
            headers=headers, params={"location": job.get("location", ""), "timeoutMs": 120000,
                                     "maxResults": 20000}, timeout=180,
        )
        r.raise_for_status()
        data = r.json()

    if "schema" not in data:
        return []
    cols = [f["name"] for f in data["schema"]["fields"]]
    rows = [dict(zip(cols, [c.get("v") for c in row["f"]])) for row in data.get("rows", [])]

    token = data.get("pageToken")
    while token:
        r = requests.get(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries/{job['jobId']}",
            headers=headers,
            params={"location": job.get("location", ""), "pageToken": token, "maxResults": 20000},
            timeout=180,
        )
        r.raise_for_status()
        page = r.json()
        rows += [dict(zip(cols, [c.get("v") for c in row["f"]])) for row in page.get("rows", [])]
        token = page.get("pageToken")

    return rows


def p_date(name, value):
    return {"name": name, "parameterType": {"type": "DATE"}, "parameterValue": {"value": value}}


def p_str(name, value):
    return {"name": name, "parameterType": {"type": "STRING"}, "parameterValue": {"value": value}}




MODELOS = [
    ("last",    "Último clique"),
    ("first",   "Primeiro clique"),
    ("ga4_dda", "Baseado em dados (GA4)"),
]


def _junta(linhas, chaves):
    out = {}
    for r in linhas:
        k = tuple(r[c] for c in chaves)
        if k in out:
            out[k]["orders"] += r["orders"]
            out[k]["revenue"] = round(out[k]["revenue"] + r["revenue"], 2)
        else:
            out[k] = r
    return list(out.values())


def gen_origin_block(creds, erp_series):
    """Origem por dia/plataforma nos tres modelos de atribuicao.

    `last` e `first` sao EXATOS por pedido: saem de order_origin_full, que tem
    1 linha por pedido do ERP. O que nenhuma rota resolveu vira "Sem origem", e
    a diferenca contra o total do ERP (a replicacao anda 1x/dia, a serie do topo
    de hora em hora) cai no mesmo balde -- o bloco fecha com o topo.

    `ga4_dda` e o modelo data-driven do proprio GA4, que **so existe agregado**:
    o credito nao vem no export do BigQuery, so na Data API, por dia x
    plataforma x source/medium e em valor fracionado. Entao aqui ele entra como
    PROPORCAO aplicada ao total do ERP, nao como contagem de pedido.
    """
    por_pedido = bq_query(creds, f"""
      SELECT CAST(order_date AS STRING) AS d, plataforma,
             canal, COALESCE(source, "(not set)") AS source, COALESCE(medium, "(not set)") AS medium,
             canal_first, COALESCE(source_first, "(not set)") AS source_first,
             COALESCE(medium_first, "(not set)") AS medium_first,
             -- `receita` (nao grand_total): a coluna vem do sub_total pela mesma
             -- formula do topo, carimbada la no orders_keys e replicada pro US.
             COUNT(*) AS pedidos, SUM(receita) AS receita
      FROM `{ORIGIN_TABLE}`
      WHERE order_date >= DATE_SUB(CURRENT_DATE("America/Sao_Paulo"), INTERVAL 120 DAY)
      GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
    """)
    dda = bq_query(creds, f"""
      SELECT CAST(order_date AS STRING) AS d, plataforma, canal,
             COALESCE(source, "(not set)") AS source, COALESCE(medium, "(not set)") AS medium,
             SUM(credito) AS credito, SUM(receita_credito) AS receita_credito
      FROM `{DDA_TABLE}`
      WHERE order_date >= DATE_SUB(CURRENT_DATE("America/Sao_Paulo"), INTERVAL 120 DAY)
      GROUP BY 1, 2, 3, 4, 5
    """)
    if not por_pedido:
        return None

    def rotula(canal):
        return "Sem origem" if canal in ("Nao identificado", None) else canal

    # {(modelo, data, plataforma): {(canal, source, medium): (pedidos, receita)}}
    tab = {}
    for r in por_pedido:
        ped, rec = int(r["pedidos"]), float(r["receita"] or 0)
        for modelo, c, sc, md in (("last",  r["canal"],       r["source"],       r["medium"]),
                                  ("first", r["canal_first"], r["source_first"], r["medium_first"])):
            canal = rotula(c)
            sm = ("(sem origem)", "(sem origem)") if canal == "Sem origem" else (sc, md)
            chave = (modelo, r["d"], r["plataforma"])
            p0, r0 = tab.setdefault(chave, {}).get((canal,) + sm, (0, 0.0))
            tab[chave][(canal,) + sm] = (p0 + ped, r0 + rec)

    # o DDA vem em credito fracionado; guarda cru e rateia depois
    credito = {}
    for r in dda:
        canal = rotula(r["canal"])
        sm = ("(sem origem)", "(sem origem)") if canal == "Sem origem" else (r["source"], r["medium"])
        chave = ("ga4_dda", r["d"], r["plataforma"])
        c0, rc0 = credito.setdefault(chave, {}).get((canal,) + sm, (0.0, 0.0))
        credito[chave][(canal,) + sm] = (c0 + float(r["credito"] or 0),
                                         rc0 + float(r["receita_credito"] or 0))

    erp = {d["date"]: d for d in erp_series}
    COBERTURA_MINIMA = 0.5   # replicacao nao passou
    ORIGEM_MINIMA = 0.2      # export do GA4 daquele dia ja foi apagado pela retencao

    # O DDA vem da Data API, que guarda agregado por 14 meses -- ele cobre dias
    # que o export do BigQuery ja apagou. Se cada modelo usasse a sua janela, o
    # total mudaria ao trocar de modelo e pareceria bug. Entao a janela e a do
    # `last` (a mais restrita) para os tres.
    out, out_sm, resumo = [], [], {}
    celulas_ok = None
    for modelo, _ in MODELOS:
        aceitas = set()
        com_origem = total_erp = 0
        for dia in sorted(d["date"] for d in erp_series):
            dia_erp = erp[dia]
            for plataforma in ("app", "web"):
                erp_pedidos = dia_erp[f"{plataforma}_orders"]
                erp_receita = dia_erp[f"{plataforma}_revenue"]
                if not erp_pedidos:
                    continue

                if celulas_ok is not None and (dia, plataforma) not in celulas_ok:
                    continue

                if modelo == "ga4_dda":
                    fatias = credito.get((modelo, dia, plataforma), {})
                    total_credito = sum(v for v, _ in fatias.values())
                    total_receita = sum(rc for _, rc in fatias.values())
                    resolvido = sum(v for (c, _, _), (v, _) in fatias.items() if c != "Sem origem")
                    if not total_credito or resolvido < total_credito * ORIGEM_MINIMA:
                        continue
                    # pedido rateado pelo credito de conversao; faturamento pelo
                    # credito de RECEITA -- se usasse o mesmo, o ticket medio
                    # sairia identico em toda linha, que e artefato do rateio.
                    itens = {k: (erp_pedidos * v / total_credito,
                                 erp_receita * rc / total_receita if total_receita else 0)
                             for k, (v, rc) in fatias.items()}
                    sobra = 0
                    total_erp += erp_pedidos
                    com_origem += erp_pedidos * resolvido / total_credito
                    aceitas.add((dia, plataforma))
                else:
                    itens = tab.get((modelo, dia, plataforma), {})
                    na_tabela = sum(p for p, _ in itens.values())
                    if na_tabela < erp_pedidos * COBERTURA_MINIMA:
                        continue
                    resolvido = sum(p for (c, _, _), (p, _) in itens.items() if c != "Sem origem")
                    if resolvido < na_tabela * ORIGEM_MINIMA:
                        continue
                    sobra = erp_pedidos - na_tabela
                    sobra_receita = max(erp_receita - sum(r for _, r in itens.values()), 0)
                    total_erp += erp_pedidos
                    com_origem += resolvido
                    aceitas.add((dia, plataforma))

                for (canal, src, med), (ped, rec) in itens.items():
                    base = {"modelo": modelo, "date": dia, "platform": plataforma, "canal": canal,
                            "orders": round(ped, 2), "revenue": round(rec, 2)}
                    out.append(dict(base))
                    out_sm.append({**base, "source": src, "medium": med})
                if sobra > 0:
                    base = {"modelo": modelo, "date": dia, "platform": plataforma,
                            "canal": "Sem origem", "orders": float(sobra),
                            "revenue": round(sobra_receita, 2)}
                    out.append(dict(base))
                    out_sm.append({**base, "source": "(sem origem)", "medium": "(sem origem)"})

        resumo[modelo] = round(com_origem / total_erp * 100, 1) if total_erp else 0
        if celulas_ok is None:
            celulas_ok = aceitas

    out = _junta(out, ("modelo", "date", "platform", "canal"))
    out_sm = _junta(out_sm, ("modelo", "date", "platform", "canal", "source", "medium"))
    dias = sorted({r["date"] for r in out})
    return {
        "models": [{"id": m, "label": lbl, "match_pct": resumo.get(m, 0)} for m, lbl in MODELOS],
        "start": dias[0] if dias else None,
        "end": dias[-1] if dias else None,
        "match_pct": resumo.get("last", 0),
        "rows": out,
        "rows_sm": out_sm,
    }


# --------------------------------------------------------------------------
# serie diaria (ERP)
# --------------------------------------------------------------------------

def gen_daily(creds):
    rows = bq_query(creds, f"""
    SELECT
      CAST(order_date AS STRING) AS order_date,
      CASE
        WHEN device IN ('iOS', 'Android') THEN 'app'
        WHEN device IN ('Mobile', 'DeskTop') THEN 'web'
        ELSE 'other'
      END AS platform,
      COUNT(*) AS orders,
      {RECEITA_SQL}
    FROM `{ERP_TABLE}`
    WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
    GROUP BY order_date, platform
    ORDER BY order_date, platform
    """)
    VAZIO = {"orders": 0, "subtotal": 0.0, "taxas": 0.0, "descontos": 0.0,
             "revenue": 0.0, "revenue_grand": 0.0}
    by_date = {}
    for row in rows:
        if row["platform"] == "other":
            continue
        d = row["order_date"]
        by_date.setdefault(d, {"date": d,
                               **{f"app_{k}": v for k, v in VAZIO.items()},
                               **{f"web_{k}": v for k, v in VAZIO.items()}})
        by_date[d][f"{row['platform']}_orders"] = int(row["orders"])
        for k, v in _receita(row).items():
            by_date[d][f"{row['platform']}_{k}"] = v
    series = [by_date[d] for d in sorted(by_date)]

    # A tabela do ERP so passa a ter volume de verdade a partir de 30/06/2026 (a
    # ingestao comecou ali); antes disso ela tem um punhado de pedidos por dia,
    # que no grafico viram semanas de linha rente ao zero. Corta esse comeco.
    MINIMO_DIA = 500
    primeiro_cheio = next((i for i, r in enumerate(series)
                           if r["app_orders"] + r["web_orders"] >= MINIMO_DIA), 0)
    series = series[primeiro_cheio:]

    # O dia corrente sempre chega pela metade (a carga do ERP atrasa ~3h), e uma
    # ultima barra despencando parece queda de vendas. O dia de hoje e a aba Intraday.
    hoje = datetime.datetime.now(BRT).date().isoformat()
    series = [r for r in series if r["date"] < hoje]

    app_orders = sum(r["app_orders"] for r in series)
    web_orders = sum(r["web_orders"] for r in series)
    app_revenue = round(sum(r["app_revenue"] for r in series), 2)
    web_revenue = round(sum(r["web_revenue"] for r in series), 2)
    total_orders = app_orders + web_orders
    total_revenue = round(app_revenue + web_revenue, 2)
    # os mesmos componentes por plataforma: o filtro App/Web do dashboard lê
    # direto daqui na primeira carga, antes de qualquer recorte de data
    composicao = {}
    for k in ("subtotal", "taxas", "descontos", "revenue_grand"):
        for p in ("app", "web"):
            composicao[f"{p}_{k}"] = round(sum(r[f"{p}_{k}"] for r in series), 2)
        composicao[f"total_{k}"] = round(composicao[f"app_{k}"] + composicao[f"web_{k}"], 2)

    return {
        "generated_at": datetime.datetime.now(BRT).isoformat(),
        "source": "grupo123-metrics.df_granular.orders",
        "period": {
            "start": series[0]["date"] if series else None,
            "end": series[-1]["date"] if series else None,
            "days": len(series),
        },
        "summary": {
            "app_orders": app_orders,
            "web_orders": web_orders,
            "app_revenue": app_revenue,
            "web_revenue": web_revenue,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            **composicao,
            "app_orders_pct": round(app_orders / total_orders * 100, 2) if total_orders else 0,
            "web_orders_pct": round(web_orders / total_orders * 100, 2) if total_orders else 0,
            "app_revenue_pct": round(app_revenue / total_revenue * 100, 2) if total_revenue else 0,
            "web_revenue_pct": round(web_revenue / total_revenue * 100, 2) if total_revenue else 0,
        },
        "series": series,
    }


# --------------------------------------------------------------------------
# export pedido a pedido (aba "Pedidos")
# --------------------------------------------------------------------------

# Quantos dias de pedido vao pro CSV. A atribuicao so existe a partir de 08/07
# (retencao do export do GA4), entao ir muito alem disso so engorda o arquivo com
# linha sem origem. 30 dias ~ 110 mil linhas, ~12 MB (a Vercel serve comprimido).
PEDIDOS_DIAS = 30

COLUNAS_PEDIDOS = [
    ("pedido",            "order_id"),
    ("data",              "d"),
    ("plataforma",        "plataforma"),
    ("subtotal",          "sub_total"),
    ("taxas",             "taxas"),
    ("descontos",         "descontos"),
    ("faturamento",       "receita"),
    ("canal_ultimo",      "canal"),
    ("origem_ultimo",     "source"),
    ("midia_ultimo",      "medium"),
    ("campanha_ultimo",   "campaign"),
    ("canal_primeiro",    "canal_first"),
    ("origem_primeiro",   "source_first"),
    ("midia_primeiro",    "medium_first"),
    ("campanha_primeiro", "campaign_first"),
    ("metodo_match",      "metodo"),
]


def gen_pedidos(creds):
    """CSV com 1 linha por pedido: valor decomposto + atribuicao dos dois modelos.

    Sai de order_origin_full, que ja e 1 linha por order_id e carrega tanto as
    parcelas do valor quanto a origem de ultimo e de primeiro clique. O modelo
    data-driven do GA4 NAO entra: ele nao existe por pedido, so agregado por dia
    x plataforma x source/medium (o credito e fracionado).

    O dia corrente fica de fora, igual a serie diaria: o ERP so e reconstruido
    1x/dia as 08:00, entao o dia de hoje sempre chegaria pela metade. Como efeito
    colateral util, o arquivo fica byte a byte igual entre as rodadas do mesmo
    dia -- o cron so gera commit quando o ERP de fato mudou.
    """
    rows = bq_query(creds, f"""
      SELECT CAST(order_date AS STRING) AS d, order_id, plataforma,
             sub_total, tax + discount AS taxas,
             IF(grand_total IS NULL, 0, sub_total + tax + discount - grand_total) AS descontos,
             receita, canal, source, medium, campaign,
             canal_first, source_first, medium_first, campaign_first, metodo
      FROM `{ORIGIN_TABLE}`
      WHERE order_date >= DATE_SUB(CURRENT_DATE("America/Sao_Paulo"), INTERVAL {PEDIDOS_DIAS} DAY)
        AND order_date < CURRENT_DATE("America/Sao_Paulo")
      ORDER BY order_date DESC, order_id
    """)

    def csv_campo(v):
        if v is None:
            return ""
        v = str(v)
        return '"' + v.replace('"', '""') + '"' if any(c in v for c in ',"\n') else v

    linhas = [",".join(rot for rot, _ in COLUNAS_PEDIDOS)]
    linhas += [",".join(csv_campo(r.get(col)) for _, col in COLUNAS_PEDIDOS) for r in rows]
    return "\n".join(linhas) + "\n", len(rows), (rows[-1]["d"] if rows else None), (rows[0]["d"] if rows else None)


# --------------------------------------------------------------------------
# intraday (GA4 em tempo real)
# --------------------------------------------------------------------------

def _hourly_from_rows(rows):
    """rows: [{hr, plataforma, pedidos, receita}] -> series por hora, acumulada."""
    cell = {}
    for r in rows:
        cell[(int(r["hr"]), r["plataforma"])] = (int(r["pedidos"]), float(r["receita"] or 0))

    out = {}
    for plataforma in ("app", "web", "total"):
        orders, revenue, orders_cum, revenue_cum = [], [], [], []
        run_o, run_r = 0, 0.0
        for h in range(24):
            if plataforma == "total":
                o = sum(cell.get((h, p), (0, 0.0))[0] for p in ("app", "web"))
                v = sum(cell.get((h, p), (0, 0.0))[1] for p in ("app", "web"))
            else:
                o, v = cell.get((h, plataforma), (0, 0.0))
            run_o += o
            run_r += v
            orders.append(o)
            revenue.append(round(v, 2))
            orders_cum.append(run_o)
            revenue_cum.append(round(run_r, 2))
        out[plataforma] = {"orders": orders, "revenue": revenue,
                           "orders_cum": orders_cum, "revenue_cum": revenue_cum}
    return out


def _cap(series, cap_hour):
    """Zera as horas que ainda nao aconteceram, pra linha de hoje nao despencar."""
    for plataforma in series:
        for key in series[plataforma]:
            series[plataforma][key] = [
                v if h <= cap_hour else None for h, v in enumerate(series[plataforma][key])
            ]
    return series


# Um mesmo pedido pode disparar purchase mais de uma vez (retry, reload da pagina
# de obrigado). Fica so o primeiro evento de cada transaction_id, senao o pedido
# entra duas vezes -- e, se os dois eventos caem em horas diferentes, nem o
# COUNT(DISTINCT) por hora resolve.
GA4_HOUR_SELECT = """
  WITH compras AS (
    SELECT
      ecommerce.transaction_id AS tid,
      IF(platform = "WEB", "web", "app") AS plataforma,
      IF(IS_NAN(ecommerce.purchase_revenue) OR IS_INF(ecommerce.purchase_revenue),
         0, ecommerce.purchase_revenue) AS receita,
      EXTRACT(HOUR FROM TIMESTAMP_MICROS(event_timestamp) AT TIME ZONE "America/Sao_Paulo") AS hr,
      ROW_NUMBER() OVER (PARTITION BY ecommerce.transaction_id ORDER BY event_timestamp) AS rn
    FROM `{table}`
    WHERE event_name = "purchase"
      AND ecommerce.transaction_id IS NOT NULL
      AND ecommerce.transaction_id NOT LIKE "(%"
  )
  SELECT hr, plataforma, COUNT(*) AS pedidos, SUM(receita) AS receita
  FROM compras WHERE rn = 1
  GROUP BY 1, 2
"""


def _dia_ga4(creds, dia):
    """Serie horaria de um dia fechado do GA4, tolerante a export atrasado.

    A tabela diaria `events_YYYYMMDD` so aparece algumas horas depois da virada;
    ate la existe `events_fresh_YYYYMMDD` com o mesmo schema. O BigQuery devolve
    404 para tabela inexistente, entao sem esse fallback o intraday quebrava
    toda manha -- e, como o tick aborta no primeiro erro, o data.json do dia
    tambem deixava de ser publicado.
    """
    sufixo = dia.strftime("%Y%m%d")
    for tabela in (f"{GA4_DATASET}.events_{sufixo}", f"{GA4_DATASET}.events_fresh_{sufixo}"):
        try:
            return bq_query(creds, GA4_HOUR_SELECT.format(table=tabela))
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
    print(f"aviso: sem tabela do GA4 para {dia} (nem events_ nem events_fresh_)", file=sys.stderr)
    return None


def gen_intraday(creds, anterior=None):
    """Hoje em tempo real (GA4 intraday) x ontem x mesmo dia da semana passada.

    As tres linhas vem do GA4 pra serem comparaveis entre si. O GA4 ve ~85% dos
    pedidos do ERP; o numero oficial do ERP entra separado, como referencia (e
    chega com ~3h de atraso, por isso nao serve pra linha "ao vivo").

    `anterior` e o intraday.json da rodada passada: ontem e semana passada nao
    mudam ao longo do dia, entao sao reaproveitados em vez de reconsultados a
    cada 10 minutos (essas duas queries custam mais que a de hoje).
    """
    agora = datetime.datetime.now(BRT)
    hoje = agora.date()
    ontem = hoje - datetime.timedelta(days=1)
    semana = hoje - datetime.timedelta(days=7)

    series = {}
    series["today"] = _cap(
        _hourly_from_rows(bq_query(creds, GA4_HOUR_SELECT.format(
            table=f"{GA4_DATASET}.events_intraday_{hoje.strftime('%Y%m%d')}"))),
        agora.hour,
    )

    reaproveitou = (
        anterior
        and anterior.get("yesterday") == ontem.isoformat()
        and anterior.get("last_week") == semana.isoformat()
        and "yesterday" in anterior.get("series", {})
        and "last_week" in anterior.get("series", {})
    )
    if reaproveitou:
        series["yesterday"] = anterior["series"]["yesterday"]
        series["last_week"] = anterior["series"]["last_week"]
    else:
        for chave, dia in (("yesterday", ontem), ("last_week", semana)):
            linhas = _dia_ga4(creds, dia)
            if linhas is not None:
                series[chave] = _hourly_from_rows(linhas)

    # numero oficial do ERP pra hoje, so como referencia (chega atrasado)
    erp = bq_query(creds, f"""
      SELECT
        COUNT(*) AS pedidos,
        {RECEITA_SQL},
        FORMAT_TIMESTAMP("%Y-%m-%dT%H:%M:%S", MAX(created_at), "America/Sao_Paulo") AS ultimo_pedido,
        FORMAT_TIMESTAMP("%Y-%m-%dT%H:%M:%S", MAX(received_at), "America/Sao_Paulo") AS ultima_carga
      FROM `{ERP_TABLE}`
      WHERE order_date = CURRENT_DATE("America/Sao_Paulo")
    """)
    erp = erp[0] if erp else {}

    # origem do dia: a tabela intraday do GA4 nao traz o traffic source resolvido,
    # entao ele e reconstruido a partir do session_start da propria sessao.
    #
    # O source/medium sai cru junto com o canal: o dashboard recategoriza por
    # conta propria (categorias.json), e o CASE abaixo e so o padrao de fabrica
    # -- sem os dois campos, a aba Intraday ficaria presa nesta taxonomia.
    origem = bq_query(creds, f"""
    WITH compras AS (
      SELECT * EXCEPT(rn) FROM (
        SELECT user_pseudo_id,
               (SELECT value.int_value FROM UNNEST(event_params) WHERE key = "ga_session_id") AS sid,
               IF(platform = "WEB", "web", "app") AS plataforma,
               ecommerce.transaction_id AS tid,
               ROW_NUMBER() OVER (PARTITION BY ecommerce.transaction_id ORDER BY event_timestamp) AS rn
        FROM `{GA4_DATASET}.events_intraday_{hoje.strftime('%Y%m%d')}`
        WHERE event_name = "purchase" AND ecommerce.transaction_id IS NOT NULL
          AND ecommerce.transaction_id NOT LIKE "(%"
      ) WHERE rn = 1
    ),
    sessoes AS (
      SELECT user_pseudo_id,
             (SELECT value.int_value FROM UNNEST(event_params) WHERE key = "ga_session_id") AS sid,
             ANY_VALUE(COALESCE(collected_traffic_source.manual_source, traffic_source.source)) AS source,
             ANY_VALUE(COALESCE(collected_traffic_source.manual_medium, traffic_source.medium)) AS medium,
             ANY_VALUE(IF(collected_traffic_source.gclid IS NOT NULL, TRUE, FALSE)) AS tem_gclid
      FROM `{GA4_DATASET}.events_intraday_{hoje.strftime('%Y%m%d')}`
      WHERE event_name = "session_start"
      GROUP BY 1, 2
    )
    SELECT
      c.plataforma,
      COALESCE(s.source, "(sem origem)") AS source,
      COALESCE(s.medium, "(sem origem)") AS medium,
      CASE
        WHEN s.tem_gclid THEN "Midia paga"
        WHEN s.medium = "metasearch" THEN "Metasearch"
        WHEN s.medium IN ("cpc", "ppc", "cpa", "paid") THEN "Midia paga"
        WHEN s.medium IN ("push", "notification") THEN "Push"
        WHEN s.medium IN ("email", "email_marketing", "sms", "chat", "radar",
                          "botaorecompra", "botaocta", "botao_comprar") THEN "CRM"
        WHEN s.medium IN ("organic", "organico") THEN "Organico"
        WHEN s.medium = "(none)" AND s.source = "(direct)" THEN "Direto"
        WHEN s.medium = "referral" THEN "Referral"
        WHEN s.source IS NULL THEN "Nao identificado"
        ELSE "Outros"
      END AS canal,
      COUNT(DISTINCT c.tid) AS pedidos
    FROM compras c
    LEFT JOIN sessoes s ON s.user_pseudo_id = c.user_pseudo_id AND s.sid = c.sid
    GROUP BY 1, 2, 3, 4
    """)

    return {
        "generated_at": agora.isoformat(),
        "timezone": "America/Sao_Paulo",
        "source": f"{GA4_DATASET} (evento purchase, tempo real)",
        "today": hoje.isoformat(),
        "yesterday": ontem.isoformat(),
        "last_week": semana.isoformat(),
        "current_hour": agora.hour,
        "hours": list(range(24)),
        "series": series,
        "origin": [{"platform": r["plataforma"], "canal": r["canal"],
                    "source": r["source"], "medium": r["medium"],
                    "orders": int(r["pedidos"])}
                   for r in origem],
        "erp": {
            "orders": int(erp["pedidos"]) if erp.get("pedidos") else 0,
            **(_receita(erp) if erp.get("pedidos") else
               {"subtotal": 0.0, "taxas": 0.0, "descontos": 0.0,
                "revenue": 0.0, "revenue_grand": 0.0}),
            "last_order_at": erp.get("ultimo_pedido"),
            "last_load_at": erp.get("ultima_carga"),
        },
    }


# --------------------------------------------------------------------------
# MaxMilhas (aba propria)
#
# A MaxMilhas NAO tem pedido nem faturamento em lugar nenhum que a gente
# alcance: `df_granular.orders` nao tem coluna de marca e nenhum pedido dela
# casa com os checkouts (73.830 dos pedidos de 30 dias casam com
# site='123milhas.com', zero com 'maxmilhas'). O export do GA4
# (analytics_327722742) tambem e so da 123 -- num dia inteiro, 15 eventos de
# ~2,5 milhoes tinham page_location de maxmilhas.
#
# Entao esta aba mede CHECKOUT, nao venda: quantas vezes alguem chegou na tela
# de pagamento, e de onde veio. O que NAO da pra fazer, e por que:
#   - receita / ticket: `checkouts` nao tem nenhuma coluna de valor;
#   - app x web: `user_agent` e nulo em 45% das linhas e nenhuma linha tem user
#     agent de app nativo (okhttp/CFNetwork/Dart) -- so navegador;
#   - visitantes unicos: `client_id` e nulo nas mesmas 45%;
#   - intraday: `checkout_received_at` mostra que a tabela e reconstruida uma
#     vez por dia as 08:00 (mesmo Dataform do ERP). Medido as 11:20 BRT, o
#     ultimo checkout na tabela era das 08:09 -- 3h10 de atraso, e nao ia mudar
#     ate a carga do dia seguinte. Uma curva "por hora" encheria ate as 8h e
#     congelaria, entao esta aba nao tem intraday.
#   - `search_id` e unico por linha (buscas == checkouts), entao nao vale card.
#
# Dia em BRT, a partir de `ts_epoch`: `checkout_date` e UTC (bate com o dia UTC
# em 99,9% das linhas e com o dia BRT em so 84%). O filtro de particao usa
# checkout_date com uma folga de um dia de cada lado, senao a conversao pra BRT
# perde as pontas. A tabela e particionada por checkout_date e clusterizada por
# site, entao a janela + o site saem baratos.
# --------------------------------------------------------------------------
MAX_TABLE = "grupo123-metrics.df_granular.checkouts"
MAX_SITE = "maxmilhas"
MAX_DIAS = 60
# a ingestao da MaxMilhas so engata em 09/07/2026; antes disso a tabela tem 1 a
# 4 checkouts por dia, que no grafico viram uma semana de linha rente ao zero
MAX_MINIMO_DIA = 500

_MAX_DIA_BRT = "DATE(TIMESTAMP_SECONDS(CAST(ts_epoch/1000 AS INT64)), 'America/Sao_Paulo')"
_MAX_JANELA = (f"site = '{MAX_SITE}' AND checkout_date BETWEEN "
               f"DATE_SUB(CURRENT_DATE('America/Sao_Paulo'), INTERVAL {MAX_DIAS + 1} DAY) "
               f"AND DATE_ADD(CURRENT_DATE('America/Sao_Paulo'), INTERVAL 1 DAY)")


def gen_maxmilhas(creds):
    hoje = datetime.datetime.now(BRT).date().isoformat()

    serie_rows = bq_query(creds, f"""
    SELECT CAST({_MAX_DIA_BRT} AS STRING) AS date,
           COUNT(*) AS checkouts,
           COUNTIF(has_tracker) AS com_tracker
    FROM `{MAX_TABLE}`
    WHERE {_MAX_JANELA}
    GROUP BY date
    ORDER BY date
    """)

    origem_rows = bq_query(creds, f"""
    SELECT CAST({_MAX_DIA_BRT} AS STRING) AS date,
           COALESCE(NULLIF(canal_url, ''), 'Sem canal') AS canal,
           COALESCE(NULLIF(url_source, ''), '(vazio)') AS source,
           COALESCE(NULLIF(url_medium, ''), '(vazio)') AS medium,
           COUNT(*) AS checkouts
    FROM `{MAX_TABLE}`
    WHERE {_MAX_JANELA}
    GROUP BY date, canal, source, medium
    """)

    # so as duas ultimas particoes: na janela inteira essa query sozinha custava
    # 74 dos 220 MB da rodada, pra devolver dois timestamps
    frescor = bq_query(creds, f"""
    SELECT FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%S',
             MAX(TIMESTAMP_SECONDS(CAST(ts_epoch/1000 AS INT64))), 'America/Sao_Paulo') AS ultimo_evento,
           FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%S', MAX(checkout_received_at), 'America/Sao_Paulo') AS ultima_carga
    FROM `{MAX_TABLE}`
    WHERE site = '{MAX_SITE}'
      AND checkout_date >= DATE_SUB(CURRENT_DATE('America/Sao_Paulo'), INTERVAL 2 DAY)
    """)

    # o dia corrente sempre chega pela metade (carga as 08:00), entao fica fora
    serie = [{"date": r["date"], "checkouts": int(r["checkouts"]),
              "com_tracker": int(r["com_tracker"])}
             for r in serie_rows if r["date"] < hoje]
    primeiro = next((i for i, r in enumerate(serie)
                     if r["checkouts"] >= MAX_MINIMO_DIA), len(serie))
    serie = serie[primeiro:]
    inicio = serie[0]["date"] if serie else hoje

    total = sum(r["checkouts"] for r in serie)
    tracker = sum(r["com_tracker"] for r in serie)

    # ---- origem: ranking de canal, tabela source/midia e a serie por dia ----
    no_periodo = [r for r in origem_rows if inicio <= r["date"] < hoje]
    por_canal, por_sm, por_dia_canal = {}, {}, {}
    for r in no_periodo:
        n = int(r["checkouts"])
        por_canal[r["canal"]] = por_canal.get(r["canal"], 0) + n
        k = (r["source"], r["medium"], r["canal"])
        por_sm[k] = por_sm.get(k, 0) + n
        dk = (r["date"], r["canal"])
        por_dia_canal[dk] = por_dia_canal.get(dk, 0) + n

    canais = sorted(({"canal": c, "checkouts": n} for c, n in por_canal.items()),
                    key=lambda x: -x["checkouts"])
    sm = sorted(({"source": s, "medium": m, "canal": c, "checkouts": n}
                 for (s, m, c), n in por_sm.items()), key=lambda x: -x["checkouts"])
    por_dia = sorted(({"date": d, "canal": c, "checkouts": n}
                      for (d, c), n in por_dia_canal.items()),
                     key=lambda x: (x["date"], -x["checkouts"]))
    sem_canal = por_canal.get("Sem canal", 0)

    return {
        "generated_at": datetime.datetime.now(BRT).isoformat(),
        "source": f"{MAX_TABLE} (site='{MAX_SITE}')",
        "metric": "checkouts",
        "freshness": frescor[0] if frescor else {},
        "period": {"start": inicio if serie else None,
                   "end": serie[-1]["date"] if serie else None,
                   "days": len(serie)},
        "summary": {
            "checkouts": total,
            "media_dia": round(total / len(serie)) if serie else 0,
            "com_tracker": tracker,
            "tracker_pct": round(tracker / total * 100, 2) if total else 0,
            "sem_canal": sem_canal,
            "sem_canal_pct": round(sem_canal / total * 100, 2) if total else 0,
        },
        "series": serie,
        "origin": {"canais": canais, "sm": sm, "por_dia": por_dia},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sa-key", default=os.environ.get(
        "SA_123_KEY", os.path.join(os.path.dirname(__file__), "..", "sa_123.json")))
    ap.add_argument("--only", choices=["all", "daily", "intraday", "pedidos", "max"], default="all",
                    help="'intraday' e a rodada barata, pra rodar de poucos em poucos minutos")
    args = ap.parse_args()

    if not os.path.exists(args.sa_key):
        print(f"chave de servico nao encontrada: {args.sa_key}", file=sys.stderr)
        sys.exit(1)

    creds = service_account.Credentials.from_service_account_file(args.sa_key, scopes=SCOPES)
    out_dir = os.path.dirname(os.path.abspath(__file__))

    def caminho(nome):
        return os.path.join(out_dir, nome)

    if args.only in ("all", "daily"):

        daily = gen_daily(creds)
        origem = gen_origin_block(creds, daily["series"])
        if origem:
            daily["origin"] = origem
            print(f"origem: {origem['start']} a {origem['end']}, "
                  f"{origem['match_pct']}% dos pedidos casados no GA4")
        with open(caminho("data.json"), "w") as f:
            json.dump(daily, f, ensure_ascii=False, indent=2)
        print("data.json atualizado:", daily["period"])

    if args.only in ("all", "daily", "pedidos"):
        csv, n, de, ate = gen_pedidos(creds)
        with open(caminho("pedidos.csv"), "w") as f:
            f.write(csv)
        print(f"pedidos.csv atualizado: {n} pedidos de {de} a {ate} ({len(csv)/1e6:.1f} MB)")

    if args.only in ("all", "max"):
        mx = gen_maxmilhas(creds)
        with open(caminho("max.json"), "w") as f:
            json.dump(mx, f, ensure_ascii=False, indent=2)
        print(f"max.json atualizado: {mx['summary']['checkouts']} checkouts em "
              f"{mx['period']['days']} dias ({mx['period']['start']} a {mx['period']['end']}), "
              f"{mx['summary']['sem_canal_pct']}% sem canal")

    if args.only in ("all", "intraday"):
        anterior = None
        if os.path.exists(caminho("intraday.json")):
            try:
                with open(caminho("intraday.json")) as f:
                    anterior = json.load(f)
            except (ValueError, OSError):
                anterior = None
        intraday = gen_intraday(creds, anterior)
        with open(caminho("intraday.json"), "w") as f:
            json.dump(intraday, f, ensure_ascii=False, indent=2)
        cut = intraday["current_hour"]
        print(f"intraday.json atualizado: {intraday['series']['today']['total']['orders_cum'][cut]} "
              f"pedidos ate {cut}h (GA4)")


if __name__ == "__main__":
    main()
