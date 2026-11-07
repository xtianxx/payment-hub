-- +goose Up
CREATE TABLE mock_transactions (
    id uuid PRIMARY KEY,
    platform_reference_id uuid NOT NULL UNIQUE,
    amount bigint NOT NULL,
    currency text NOT NULL,
    result text NOT NULL,
    processed_at timestamptz NOT NULL,
    CONSTRAINT mock_transactions_result_check CHECK (
        result IN ('SUCCEEDED', 'FAILED')
    )
);
