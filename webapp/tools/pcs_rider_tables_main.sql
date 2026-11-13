-- Target: main database only. Manual operator execution; no backfill.
BEGIN;

CREATE TABLE pcs_riders (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP,
    rider_slug VARCHAR(255) NOT NULL UNIQUE,
    profile_url VARCHAR(1000) NOT NULL,
    display_name VARCHAR(255),
    birth_date DATE,
    nationality VARCHAR(100),
    source_payload JSON,
    last_synced_at TIMESTAMP NOT NULL
);
CREATE INDEX ix_pcs_riders_birth_date ON pcs_riders (birth_date);
CREATE INDEX ix_pcs_riders_nationality ON pcs_riders (nationality);

CREATE TABLE pcs_rider_results (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP,
    rider_id INTEGER NOT NULL REFERENCES pcs_riders(id) ON DELETE CASCADE,
    source_result_key VARCHAR(200) NOT NULL,
    season INTEGER,
    result_date DATE,
    race_id VARCHAR(1000),
    race_url VARCHAR(1000) NOT NULL,
    race_name VARCHAR(255),
    race_category VARCHAR(100),
    race_type VARCHAR(100),
    stage_number INTEGER,
    stage_name VARCHAR(255),
    row_type VARCHAR(40) NOT NULL,
    result_position VARCHAR(40),
    participation_status VARCHAR(40) NOT NULL,
    confirmed_participation BOOLEAN NOT NULL,
    source_payload JSON,
    last_synced_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_pcs_rider_result_key UNIQUE (rider_id, source_result_key)
);
CREATE INDEX ix_pcs_rider_results_season ON pcs_rider_results (season);
CREATE INDEX ix_pcs_rider_result_rider_date ON pcs_rider_results (rider_id, result_date);

COMMIT;
