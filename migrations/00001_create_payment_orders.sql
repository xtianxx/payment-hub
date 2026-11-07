-- +goose Up
CREATE TABLE payment_orders (
    id uuid PRIMARY KEY,
    client_id text COLLATE "C" NOT NULL,
    business_order_no text COLLATE "C" NOT NULL,
    amount bigint NOT NULL,
    currency text NOT NULL,
    result text NOT NULL,
    mock_transaction_id uuid UNIQUE,
    registered_at timestamptz NOT NULL,
    confirmed_at timestamptz,
    CONSTRAINT payment_orders_client_id_check CHECK (
        char_length(client_id) BETWEEN 1 AND 64
        AND client_id !~ '[^A-Za-z0-9_-]'
    ),
    CONSTRAINT payment_orders_business_order_no_check CHECK (
        char_length(business_order_no) BETWEEN 1 AND 64
        AND business_order_no !~ '[^A-Za-z0-9_-]'
    ),
    CONSTRAINT payment_orders_amount_check CHECK (
        amount >= 1 AND amount <= 1000000
    ),
    CONSTRAINT payment_orders_currency_check CHECK (
        currency = 'CNY'
    ),
    CONSTRAINT payment_orders_result_check CHECK (
        result IN ('UNKNOWN', 'SUCCEEDED', 'FAILED')
    ),
    CONSTRAINT payment_orders_result_confirmed_at_check CHECK (
        (result = 'UNKNOWN' AND confirmed_at IS NULL)
        OR (result IN ('SUCCEEDED', 'FAILED') AND confirmed_at IS NOT NULL)
    ),
    CONSTRAINT payment_orders_business_key UNIQUE (client_id, business_order_no)
);
