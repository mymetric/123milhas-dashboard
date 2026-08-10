#!/usr/bin/env python3
"""Gera os snapshots do dashboard 123 Milhas.

  data.json      serie diaria (90 dias) de pedidos/faturamento app x web, do ERP,
                 + bloco de origem de sessao (GA4 last click)
  intraday.json  hoje x ontem x mesmo dia da semana passada, por hora, em tempo
                 real (GA4), com a origem do dia e o numero oficial do ERP

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
# bigquery (nao readonly): o refresh da origem grava em df_granular_us.order_origin
SCOPES = ["https://www.googleapis.com/auth/bigquery"]
BRT = datetime.timezone(datetime.timedelta(hours=-3))

GA4_DATASET = "grupo123-metrics.analytics_327722742"
ORIGIN_TABLE = "grupo123-metrics.df_granular_us.order_origin"
ERP_TABLE = "grupo123-metrics.df_granular.orders"

# Quantos dias reprocessar de origem a cada rodada. O GA4 fecha a tabela do dia
# so na madrugada seguinte, e ainda corrige numeros por ~2 dias.
ORIGIN_REFRESH_DAYS = 4


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


# --------------------------------------------------------------------------
# origem de sessao (GA4 -> tabela materializada em US)
# --------------------------------------------------------------------------

CANAL_CASE = """
  CASE
    WHEN medium = "metasearch" THEN "Metasearch"
    WHEN medium IN ("cpc", "ppc", "cpa", "paid") THEN "Midia paga"
    WHEN medium IN ("push", "notification") THEN "Push"
    WHEN medium IN ("email", "email_marketing", "sms", "chat", "radar",
                    "botaorecompra", "botaocta", "botao_comprar") THEN "CRM"
    WHEN medium IN ("organic", "organico") THEN "Organico"
    WHEN medium = "(none)" AND source = "(direct)" THEN "Direto"
    WHEN ga_group IN ("Organic Social", "Paid Social") THEN "Social"
    WHEN medium = "referral" THEN "Referral"
    WHEN source IS NULL OR medium IS NULL OR medium = "(not set)" THEN "Nao identificado"
    ELSE "Outros"
  END
"""


def refresh_origin(creds, days=ORIGIN_REFRESH_DAYS):
    """Recarrega as ultimas particoes de order_origin a partir do export do GA4.

    A tabela do GA4 e apagada depois de ~36 dias; order_origin e o unico registro
    historico de origem que sobra, entao ela so acumula, nunca e truncada.
    """
    hoje = datetime.datetime.now(BRT).date()
    de = hoje - datetime.timedelta(days=days)
    ate = hoje - datetime.timedelta(days=1)  # o dia corrente ainda nao fechou no GA4

    params = [p_date("de", de.isoformat()), p_date("ate", ate.isoformat()),
              p_str("sufixo_de", de.strftime("%Y%m%d")), p_str("sufixo_ate", ate.strftime("%Y%m%d"))]

    bq_query(creds, f"DELETE FROM `{ORIGIN_TABLE}` WHERE order_date BETWEEN @de AND @ate", params)
    bq_query(creds, f"""
    INSERT INTO `{ORIGIN_TABLE}` (order_date, order_id, plataforma, source, medium, campaign, canal)
    WITH purchases AS (
      SELECT
        PARSE_DATE("%Y%m%d", event_date) AS order_date,
        REPLACE(ecommerce.transaction_id, "f-", "") AS order_id,
        IF(platform = "WEB", "web", "app") AS plataforma,
        session_traffic_source_last_click.cross_channel_campaign.source AS source,
        session_traffic_source_last_click.cross_channel_campaign.medium AS medium,
        session_traffic_source_last_click.cross_channel_campaign.campaign_name AS campaign,
        session_traffic_source_last_click.cross_channel_campaign.default_channel_group AS ga_group,
        ROW_NUMBER() OVER (PARTITION BY ecommerce.transaction_id ORDER BY event_timestamp) AS rn
      FROM `{GA4_DATASET}.events_*`
      WHERE _TABLE_SUFFIX BETWEEN @sufixo_de AND @sufixo_ate
        AND event_name = "purchase"
        AND ecommerce.transaction_id IS NOT NULL
        AND ecommerce.transaction_id NOT LIKE "(%"
    )
    SELECT order_date, order_id, plataforma, source, medium, campaign, {CANAL_CASE} AS canal
    FROM purchases
    WHERE rn = 1 AND order_date BETWEEN @de AND @ate
    """, params)
    return {"de": de.isoformat(), "ate": ate.isoformat()}


def gen_origin_block(creds, erp_series):
    """Distribuicao de origem por dia/plataforma, ja aplicada aos totais do ERP.

    O GA4 nao ve 100% dos pedidos (~85%), entao ele nao entra como volume: entra
    como *proporcao*. Os pedidos e a receita mostrados no bloco de origem sao os
    do ERP, rateados pela participacao de cada canal medida no GA4 daquele mesmo
    dia e plataforma. Assim o bloco fecha com os totais do topo do dashboard.
    """
    rows = bq_query(creds, f"""
      SELECT CAST(order_date AS STRING) AS d, plataforma, canal, COUNT(*) AS pedidos
      FROM `{ORIGIN_TABLE}`
      WHERE order_date >= DATE_SUB(CURRENT_DATE("America/Sao_Paulo"), INTERVAL 120 DAY)
      GROUP BY 1, 2, 3
    """)
    if not rows:
        return None

    # {(data, plataforma): {canal: pedidos_ga4}}
    ga4 = {}
    for r in rows:
        ga4.setdefault((r["d"], r["plataforma"]), {})[r["canal"]] = int(r["pedidos"])

    erp = {d["date"]: d for d in erp_series}

    # Dias em que o GA4 mal registrou compra (export incompleto, tag fora do ar)
    # nao podem ratear o dia inteiro em cima de um punhado de pedidos.
    MINIMO_GA4 = 20

    # So entram no bloco os dias em que o GA4 existe; antes disso o export ja foi
    # apagado pela retencao e o rateio nao teria base nenhuma.
    dias_com_ga4 = {d for d, _ in ga4}
    primeiro = min(dias_com_ga4) if dias_com_ga4 else None

    out = []
    coberto_ga4 = 0
    coberto_erp = 0
    for dia in sorted(d["date"] for d in erp_series):
        if primeiro is None or dia < primeiro:
            continue
        dia_erp = erp[dia]
        for plataforma in ("app", "web"):
            erp_pedidos = dia_erp[f"{plataforma}_orders"]
            erp_receita = dia_erp[f"{plataforma}_revenue"]
            if not erp_pedidos:
                continue
            canais = ga4.get((dia, plataforma), {})
            total_ga4 = sum(canais.values())
            if total_ga4 < MINIMO_GA4:
                # sem base pra ratear: o volume do ERP aparece, mas como sem origem
                out.append({"date": dia, "platform": plataforma, "canal": "Sem dado de origem",
                            "orders": float(erp_pedidos), "revenue": erp_receita})
                coberto_erp += erp_pedidos
                continue
            coberto_ga4 += total_ga4
            coberto_erp += erp_pedidos
            for canal, n in canais.items():
                share = n / total_ga4
                out.append({
                    "date": dia,
                    "platform": plataforma,
                    "canal": canal,
                    "orders": round(erp_pedidos * share, 2),
                    "revenue": round(erp_receita * share, 2),
                })

    dias = sorted({r["date"] for r in out})
    return {
        "source": "GA4 (analytics_327722742) purchase + session_traffic_source_last_click",
        "start": dias[0] if dias else None,
        "end": dias[-1] if dias else None,
        "match_pct": round(coberto_ga4 / coberto_erp * 100, 1) if coberto_erp else 0,
        "rows": out,
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
      SUM(grand_total) AS revenue
    FROM `{ERP_TABLE}`
    WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
    GROUP BY order_date, platform
    ORDER BY order_date, platform
    """)
    by_date = {}
    for row in rows:
        if row["platform"] == "other":
            continue
        d = row["order_date"]
        by_date.setdefault(d, {"date": d, "app_orders": 0, "app_revenue": 0.0,
                               "web_orders": 0, "web_revenue": 0.0})
        by_date[d][f"{row['platform']}_orders"] = int(row["orders"])
        by_date[d][f"{row['platform']}_revenue"] = round(float(row["revenue"]), 2)
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
            "app_orders_pct": round(app_orders / total_orders * 100, 2) if total_orders else 0,
            "web_orders_pct": round(web_orders / total_orders * 100, 2) if total_orders else 0,
            "app_revenue_pct": round(app_revenue / total_revenue * 100, 2) if total_revenue else 0,
            "web_revenue_pct": round(web_revenue / total_revenue * 100, 2) if total_revenue else 0,
        },
        "series": series,
    }


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
            series[chave] = _hourly_from_rows(bq_query(creds, GA4_HOUR_SELECT.format(
                table=f"{GA4_DATASET}.events_{dia.strftime('%Y%m%d')}")))

    # numero oficial do ERP pra hoje, so como referencia (chega atrasado)
    erp = bq_query(creds, f"""
      SELECT
        COUNT(*) AS pedidos,
        SUM(grand_total) AS receita,
        FORMAT_TIMESTAMP("%Y-%m-%dT%H:%M:%S", MAX(created_at), "America/Sao_Paulo") AS ultimo_pedido,
        FORMAT_TIMESTAMP("%Y-%m-%dT%H:%M:%S", MAX(received_at), "America/Sao_Paulo") AS ultima_carga
      FROM `{ERP_TABLE}`
      WHERE order_date = CURRENT_DATE("America/Sao_Paulo")
    """)
    erp = erp[0] if erp else {}

    # origem do dia: a tabela intraday do GA4 nao traz o traffic source resolvido,
    # entao ele e reconstruido a partir do session_start da propria sessao.
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
    GROUP BY 1, 2
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
        "origin": [{"platform": r["plataforma"], "canal": r["canal"], "orders": int(r["pedidos"])}
                   for r in origem],
        "erp": {
            "orders": int(erp["pedidos"]) if erp.get("pedidos") else 0,
            "revenue": round(float(erp["receita"]), 2) if erp.get("receita") else 0.0,
            "last_order_at": erp.get("ultimo_pedido"),
            "last_load_at": erp.get("ultima_carga"),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sa-key", default=os.environ.get(
        "SA_123_KEY", os.path.join(os.path.dirname(__file__), "..", "sa_123.json")))
    ap.add_argument("--only", choices=["all", "daily", "intraday"], default="all",
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
        janela = refresh_origin(creds)
        print(f"order_origin recarregado: {janela['de']} a {janela['ate']}")

        daily = gen_daily(creds)
        origem = gen_origin_block(creds, daily["series"])
        if origem:
            daily["origin"] = origem
            print(f"origem: {origem['start']} a {origem['end']}, "
                  f"{origem['match_pct']}% dos pedidos casados no GA4")
        with open(caminho("data.json"), "w") as f:
            json.dump(daily, f, ensure_ascii=False, indent=2)
        print("data.json atualizado:", daily["period"])

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
