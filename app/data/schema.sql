-- StockFlow Analysis — Database Schema
-- Run this in your Supabase SQL editor (or any PostgreSQL instance)
-- Table creation order matters — do not reorder.
-- price_data and divergence_scores reference stocks(symbol),
-- so stocks must exist and be populated before those tables can receive rows.

CREATE TABLE IF NOT EXISTS stocks (
  symbol        VARCHAR(20)   PRIMARY KEY,
  company_name  VARCHAR(150)  NOT NULL,
  sector        VARCHAR(100),
  isin          VARCHAR(12),
  in_watchlist  BOOLEAN       DEFAULT FALSE,
  created_at    TIMESTAMPTZ   DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fii_dii_flows (
  trade_date   DATE           PRIMARY KEY,
  fii_buy      NUMERIC(14,2),
  fii_sell     NUMERIC(14,2),
  fii_net      NUMERIC(14,2),
  dii_buy      NUMERIC(14,2),
  dii_sell     NUMERIC(14,2),
  dii_net      NUMERIC(14,2),
  nifty_close  NUMERIC(12,2),
  created_at   TIMESTAMPTZ    DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS price_data (
  symbol       VARCHAR(20)   NOT NULL REFERENCES stocks(symbol),
  trade_date   DATE          NOT NULL,
  open         NUMERIC(12,2),
  high         NUMERIC(12,2),
  low          NUMERIC(12,2),
  close        NUMERIC(12,2) NOT NULL,
  volume       BIGINT,
  created_at   TIMESTAMPTZ   DEFAULT NOW(),
  PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_price_data_date   ON price_data (trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_price_data_symbol ON price_data (symbol);

CREATE TABLE IF NOT EXISTS divergence_scores (
  symbol        VARCHAR(20)   NOT NULL REFERENCES stocks(symbol),
  trade_date    DATE          NOT NULL,
  window_days   INTEGER       NOT NULL,
  score         NUMERIC(8,4),
  direction     VARCHAR(50),
  is_flagged    BOOLEAN       DEFAULT FALSE,
  created_at    TIMESTAMPTZ   DEFAULT NOW(),
  PRIMARY KEY (symbol, trade_date, window_days)
);
CREATE INDEX IF NOT EXISTS idx_div_scores_date    ON divergence_scores (trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_div_scores_flagged ON divergence_scores (is_flagged) WHERE is_flagged = TRUE;

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id              SERIAL         PRIMARY KEY,
  started_at      TIMESTAMPTZ    DEFAULT NOW(),
  finished_at     TIMESTAMPTZ,
  status          VARCHAR(100),
  stocks_fetched  INTEGER        DEFAULT 0,
  stocks_scored   INTEGER        DEFAULT 0,
  stocks_flagged  INTEGER        DEFAULT 0,
  error_message   TEXT,
  triggered_by    VARCHAR(50)    DEFAULT 'cron'
);
