-- Série diária de pedidos e faturamento, últimos 90 dias, por plataforma (app x web)
-- app = iOS/Android · web = Mobile/DeskTop
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
ORDER BY order_date, platform;
