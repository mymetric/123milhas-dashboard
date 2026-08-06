#!/usr/bin/env python3
"""Gera data.json (série diária, 90 dias) e intraday.json (hoje x ontem x mesmo
dia da semana passada, por hora, acumulado) a partir de
`grupo123-metrics.df_granular.orders`.

Uso:
  python3 refresh_data.py --sa-key /path/sa_123.json

Sem parâmetro --sa-key, tenta $SA_123_KEY ou ../sa_123.json.
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
SCOPES = ["https://www.googleapis.com/auth/bigquery.readonly"]


def bq_query(creds, sql, max_results=5000):
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    r = requests.post(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries",
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
        json={"query": sql, "useLegacySql": False, "maxResults": max_results},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if "rows" not in data:
        return []
    cols = [f["name"] for f in data["schema"]["fields"]]
    return [dict(zip(cols, [c.get("v") for c in row["f"]])) for row in data["rows"]]


def gen_daily(creds):
    sql = """
    SELECT
      order_date,
      CASE
        WHEN device IN ('iOS', 'Android') THEN 'app'
        WHEN device IN ('Mobile', 'DeskTop') THEN 'web'
        ELSE 'other'
      END AS platform,
      COUNT(*) AS orders,
      SUM(grand_total) AS revenue
    FROM `grupo123-metrics.df_granular.orders`
    WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
    GROUP BY order_date, platform
    ORDER BY order_date, platform
    """
    rows = bq_query(creds, sql)
    by_date = {}
    for row in rows:
        if row["platform"] == "other":
            continue
        d = row["order_date"]
        by_date.setdefault(d, {"date": d, "app_orders": 0, "app_revenue": 0.0, "web_orders": 0, "web_revenue": 0.0})
        by_date[d][f"{row['platform']}_orders"] = int(row["orders"])
        by_date[d][f"{row['platform']}_revenue"] = round(float(row["revenue"]), 2)
    series = [by_date[d] for d in sorted(by_date)]

    app_orders = sum(r["app_orders"] for r in series)
    web_orders = sum(r["web_orders"] for r in series)
    app_revenue = round(sum(r["app_revenue"] for r in series), 2)
    web_revenue = round(sum(r["web_revenue"] for r in series), 2)
    total_orders = app_orders + web_orders
    total_revenue = round(app_revenue + web_revenue, 2)

    return {
        "generated_at": datetime.date.today().isoformat(),
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


def gen_intraday(creds):
    # "dia" segue order_date (mesmo campo usado no data.json/query.sql) — não
    # DATE(created_at) em BRT, que diverge perto da virada UTC (order_date
    # parece ser atribuído em UTC). A hora dentro do dia usa created_at em BRT.
    fresh = bq_query(creds, "SELECT MAX(order_date) AS d, MAX(created_at) AS ts FROM `grupo123-metrics.df_granular.orders`")[0]
    today = datetime.date.fromisoformat(fresh["d"])
    yesterday = today - datetime.timedelta(days=1)
    last_week = today - datetime.timedelta(days=7)
    last_created_brt = datetime.datetime.fromtimestamp(float(fresh["ts"]), datetime.timezone(datetime.timedelta(hours=-3)))

    dates = [today.isoformat(), yesterday.isoformat(), last_week.isoformat()]
    sql = f"""
    SELECT
      order_date AS d,
      EXTRACT(HOUR FROM DATETIME(created_at, "America/Sao_Paulo")) AS hr,
      COUNT(*) AS orders,
      SUM(grand_total) AS revenue
    FROM `grupo123-metrics.df_granular.orders`
    WHERE order_date IN ({', '.join('"%s"' % d for d in dates)})
    GROUP BY d, hr
    ORDER BY d, hr
    """
    rows = bq_query(creds, sql)
    by_date_hour = {}
    for row in rows:
        by_date_hour.setdefault(row["d"], {})[int(row["hr"])] = {
            "orders": int(row["orders"]),
            "revenue": round(float(row["revenue"]), 2),
        }
    # cap "hoje" na hora do dado mais recente de fato ingerido (não no
    # relogio de agora) -- a fonte pode estar atrasada, e um corte no
    # horario atual faria o grafico parecer que as vendas zeraram.
    current_hour = last_created_brt.hour if last_created_brt.date() == today else 23

    def build_series(date_str, cap_hour=None):
        orders, revenue, orders_cum, revenue_cum = [], [], [], []
        run_o, run_r = 0, 0.0
        for h in range(24):
            if cap_hour is not None and h > cap_hour:
                orders.append(None)
                revenue.append(None)
                orders_cum.append(None)
                revenue_cum.append(None)
                continue
            cell = by_date_hour.get(date_str, {}).get(h, {"orders": 0, "revenue": 0.0})
            run_o += cell["orders"]
            run_r += cell["revenue"]
            orders.append(cell["orders"])
            revenue.append(round(cell["revenue"], 2))
            orders_cum.append(run_o)
            revenue_cum.append(round(run_r, 2))
        return {"orders": orders, "revenue": revenue, "orders_cum": orders_cum, "revenue_cum": revenue_cum}

    return {
        "generated_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))).isoformat(),
        "last_data_at": last_created_brt.isoformat(),
        "timezone": "America/Sao_Paulo",
        "today": today.isoformat(),
        "yesterday": yesterday.isoformat(),
        "last_week": last_week.isoformat(),
        "current_hour": current_hour,
        "hours": list(range(24)),
        "series": {
            "today": build_series(today.isoformat(), cap_hour=current_hour),
            "yesterday": build_series(yesterday.isoformat()),
            "last_week": build_series(last_week.isoformat()),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sa-key", default=os.environ.get("SA_123_KEY", os.path.join(os.path.dirname(__file__), "..", "sa_123.json")))
    args = ap.parse_args()

    if not os.path.exists(args.sa_key):
        print(f"chave de servico nao encontrada: {args.sa_key}", file=sys.stderr)
        sys.exit(1)

    creds = service_account.Credentials.from_service_account_file(args.sa_key, scopes=SCOPES)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    daily = gen_daily(creds)
    with open(os.path.join(out_dir, "data.json"), "w") as f:
        json.dump(daily, f, ensure_ascii=False, indent=2)
    print("data.json atualizado:", daily["period"])

    intraday = gen_intraday(creds)
    with open(os.path.join(out_dir, "intraday.json"), "w") as f:
        json.dump(intraday, f, ensure_ascii=False, indent=2)
    print("intraday.json atualizado: hora atual", intraday["current_hour"])


if __name__ == "__main__":
    main()
