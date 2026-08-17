-- Série diária de pedidos e faturamento, últimos 90 dias, por plataforma (app x web)
-- app = iOS/Android · web = Mobile/DeskTop
--
-- Faturamento NÃO sai do grand_total (ver README):
--   * o campo `discount` da tabela é ACRÉSCIMO, não desconto — o desconto real
--     é a sobra entre sub_total + tax + discount e o grand_total;
--   * 98.770 pedidos de 30/06 a 06/07/2026 têm grand_total NULL com
--     sub_total/tax preenchidos, e somar grand_total zera aqueles dias.
-- Então: faturamento = sub_total + (tax + discount) - desconto.
SELECT
  order_date,
  CASE
    WHEN device IN ('iOS', 'Android') THEN 'app'
    WHEN device IN ('Mobile', 'DeskTop') THEN 'web'
    ELSE 'other'
  END AS platform,
  COUNT(*) AS orders,
  SUM(sub_total) AS subtotal,
  SUM(tax + discount) AS taxas,
  SUM(IF(grand_total IS NULL, 0, sub_total + tax + discount - grand_total)) AS descontos,
  SUM(sub_total) + SUM(tax + discount)
    - SUM(IF(grand_total IS NULL, 0, sub_total + tax + discount - grand_total)) AS revenue,
  SUM(grand_total) AS grand_total_erp
FROM `grupo123-metrics.df_granular.orders`
WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
GROUP BY order_date, platform
ORDER BY order_date, platform;
