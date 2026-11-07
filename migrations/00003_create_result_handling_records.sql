-- +goose Up
CREATE TABLE result_handling_records (
    id uuid PRIMARY KEY,
    payment_order_id uuid NOT NULL,
    request_id uuid NOT NULL,
    source text NOT NULL,
    observed_at timestamptz NOT NULL,
    evidence jsonb NOT NULL,
    result_before text NOT NULL,
    result_after text NOT NULL,
    decision text NOT NULL,
    reason_code text,
    reason_message text,
    CONSTRAINT result_handling_records_payment_order_fk
        FOREIGN KEY (payment_order_id) REFERENCES payment_orders (id) ON DELETE RESTRICT,
    CONSTRAINT result_handling_records_source_check CHECK (
        source IN ('PAYMENT', 'QUERY')
    ),
    CONSTRAINT result_handling_records_evidence_check CHECK (
        jsonb_typeof(evidence) = 'object'
    ),
    CONSTRAINT result_handling_records_result_before_check CHECK (
        result_before IN ('UNKNOWN', 'SUCCEEDED', 'FAILED')
    ),
    CONSTRAINT result_handling_records_result_after_check CHECK (
        result_after IN ('UNKNOWN', 'SUCCEEDED', 'FAILED')
    ),
    CONSTRAINT result_handling_records_decision_check CHECK (
        decision IN ('ACCEPT_CONFIRMATION', 'KEEP_UNKNOWN', 'KEEP_CONFIRMED', 'REJECT_EVIDENCE')
    ),
    CONSTRAINT result_handling_records_reason_code_check CHECK (
        reason_code IS NULL OR char_length(reason_code) BETWEEN 1 AND 64
    ),
    CONSTRAINT result_handling_records_reason_message_check CHECK (
        reason_message IS NULL OR char_length(reason_message) <= 512
    )
);

CREATE INDEX result_handling_records_payment_order_history_idx
    ON result_handling_records (payment_order_id, observed_at, id);
