-- Read-only verification for one payment-create load stage.
-- psql variables:
--   client_id      verified client identity
--   prefix         exact alphanumeric order-number prefix (no wildcard)
--   expected_count number of HTTP requests k6 actually issued for the stage
--   seq_limit      highest allowed iteration sequence + 1; when drops occurred
--                  this may exceed expected_count because started iterations can
--                  have sparse sequence numbers
--   exact          1 only when the response set was clean (no dropped
--                  iterations, no timeout/transport/HTTP/business failure)
--
-- All input order numbers must be prefix + exactly 8 decimal digits. The SQL
-- presents anomalies and exact-set differences; the Python runner owns the
-- process exit gate. When exact=0 the runner accepts rows <= expected_count
-- because a dropped iteration never reaches the server and a timed-out request
-- may or may not have created an order. Internal consistency is still required.
-- This transaction never writes.
\set ON_ERROR_STOP on
\pset footer off
BEGIN TRANSACTION READ ONLY;

\echo == summary ==
WITH scoped AS (
    SELECT p.*, substring(p.business_order_no FROM length(:'prefix') + 1) AS suffix
    FROM payment_orders p
    WHERE p.client_id = :'client_id'
      AND starts_with(p.business_order_no, :'prefix')
), parsed AS (
    SELECT scoped.*,
           CASE WHEN suffix ~ '^[0-9]{8}$' THEN suffix::int END AS seq
    FROM scoped
)
SELECT 'summary' AS row_kind,
       count(*) AS rows,
       count(DISTINCT business_order_no) AS distinct_orders,
       count(*) FILTER (WHERE result = 'SUCCEEDED') AS succeeded,
       count(*) FILTER (WHERE amount = 100) AS amount_100,
       count(*) FILTER (WHERE currency = 'CNY') AS currency_cny,
       count(*) FILTER (WHERE confirmed_at IS NOT NULL) AS confirmed,
       count(*) FILTER (WHERE mock_transaction_id IS NOT NULL) AS mock_linked,
       count(*) FILTER (WHERE suffix ~ '^[0-9]{8}$') AS seq_format_ok,
       count(*) FILTER (WHERE seq BETWEEN 0 AND :seq_limit - 1) AS seq_in_range,
       min(seq) AS min_seq,
       max(seq) AS max_seq
FROM parsed;

\echo == mock_mismatch ==
WITH scoped AS (
    SELECT p.* FROM payment_orders p
    WHERE p.client_id = :'client_id' AND starts_with(p.business_order_no, :'prefix')
)
SELECT count(*) AS mock_mismatch
FROM scoped p
LEFT JOIN mock_transactions m ON m.id = p.mock_transaction_id
WHERE m.id IS NULL
   OR m.result <> 'SUCCEEDED'
   OR m.amount <> p.amount
   OR m.currency <> p.currency
   OR m.platform_reference_id <> p.id;

\echo == handling_mismatch ==
WITH scoped AS (
    SELECT p.id FROM payment_orders p
    WHERE p.client_id = :'client_id' AND starts_with(p.business_order_no, :'prefix')
), handling AS (
    SELECT p.id,
           count(r.id) AS n,
           count(*) FILTER (
               WHERE r.source = 'PAYMENT'
                 AND r.result_after = 'SUCCEEDED'
                 AND r.decision = 'ACCEPT_CONFIRMATION'
           ) AS accepted
    FROM scoped p
    LEFT JOIN result_handling_records r ON r.payment_order_id = p.id
    GROUP BY p.id
)
SELECT count(*) AS handling_mismatch
FROM handling
WHERE n <> 1 OR accepted <> 1;

\echo == duplicate_groups ==
WITH scoped AS (
    SELECT business_order_no FROM payment_orders p
    WHERE p.client_id = :'client_id' AND starts_with(p.business_order_no, :'prefix')
)
SELECT count(*) AS duplicate_groups
FROM (SELECT business_order_no FROM scoped GROUP BY 1 HAVING count(*) > 1) d;

\echo == anomaly_rows ==
WITH scoped AS (
    SELECT p.*, substring(p.business_order_no FROM length(:'prefix') + 1) AS suffix
    FROM payment_orders p
    WHERE p.client_id = :'client_id' AND starts_with(p.business_order_no, :'prefix')
), parsed AS (
    SELECT scoped.*,
           CASE WHEN suffix ~ '^[0-9]{8}$' THEN suffix::int END AS seq
    FROM scoped
)
SELECT p.business_order_no, p.id::text, p.result, p.amount, p.currency,
       (p.confirmed_at IS NOT NULL)::text AS confirmed,
       p.mock_transaction_id::text,
       m.result AS mock_result,
       m.amount AS mock_amount,
       m.currency AS mock_currency,
       (m.platform_reference_id = p.id)::text AS mock_reference_matches,
       (SELECT count(*) FROM result_handling_records r WHERE r.payment_order_id = p.id) AS handling_count,
       p.seq
FROM parsed p
LEFT JOIN mock_transactions m ON m.id = p.mock_transaction_id
WHERE p.result <> 'SUCCEEDED'
   OR p.amount <> 100
   OR p.currency <> 'CNY'
   OR p.confirmed_at IS NULL
   OR p.mock_transaction_id IS NULL
   OR m.id IS NULL
   OR m.result <> 'SUCCEEDED'
   OR m.amount <> p.amount
   OR m.currency <> p.currency
   OR m.platform_reference_id <> p.id
   OR (SELECT count(*) FROM result_handling_records r WHERE r.payment_order_id = p.id) <> 1
   OR p.seq IS NULL
   OR p.seq < 0 OR p.seq >= :seq_limit
ORDER BY p.business_order_no;

\echo == missing_expected_orders ==
WITH scoped AS (
    SELECT p.*, substring(p.business_order_no FROM length(:'prefix') + 1) AS suffix
    FROM payment_orders p
    WHERE p.client_id = :'client_id' AND starts_with(p.business_order_no, :'prefix')
), parsed AS (
    SELECT scoped.*,
           CASE WHEN suffix ~ '^[0-9]{8}$' THEN suffix::int END AS seq
    FROM scoped
), expected AS (
    SELECT generate_series(0, :expected_count - 1) AS seq WHERE :'exact' = '1'
)
SELECT :'prefix' || lpad(e.seq::text, 8, '0') AS missing_order
FROM expected e
LEFT JOIN parsed p ON p.seq = e.seq
WHERE p.seq IS NULL
ORDER BY e.seq;

\echo == extra_or_out_of_range_orders ==
WITH scoped AS (
    SELECT p.*, substring(p.business_order_no FROM length(:'prefix') + 1) AS suffix
    FROM payment_orders p
    WHERE p.client_id = :'client_id' AND starts_with(p.business_order_no, :'prefix')
), parsed AS (
    SELECT scoped.*,
           CASE WHEN suffix ~ '^[0-9]{8}$' THEN suffix::int END AS seq
    FROM scoped
)
SELECT p.business_order_no, p.seq
FROM parsed p
WHERE p.seq IS NULL OR p.seq < 0 OR p.seq >= :seq_limit
ORDER BY p.business_order_no;

\echo == done ==
COMMIT;
