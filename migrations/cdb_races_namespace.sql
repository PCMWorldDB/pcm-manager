-- Target: each namespace database ONLY, never main.
-- Back up the namespace and stop web/worker writes before running manually.
-- Review the target connection before executing. No automatic migration is used.
BEGIN;

CREATE TABLE race_class (
    id SERIAL PRIMARY KEY,
    name_string_id INTEGER,
    constant VARCHAR(100),
    max_teams INTEGER,
    min_riders INTEGER,
    max_riders INTEGER,
    sort_order INTEGER,
    procyclist_pass INTEGER,
    calendar_color VARCHAR(255),
    division_id INTEGER,
    cost INTEGER,
    material VARCHAR(500),
    is_stage_race BOOLEAN,
    is_deleted BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX ix_race_class_division_id ON race_class (division_id);

CREATE TABLE race (
    id SERIAL PRIMARY KEY,
    race_name VARCHAR(500),
    abbreviation VARCHAR(255),
    filename VARCHAR(500),
    country_id INTEGER,
    race_class_id INTEGER,
    first_stage_id INTEGER,
    last_stage_id INTEGER,
    popularity DOUBLE PRECISION,
    number_stages INTEGER,
    organizer_email VARCHAR(500),
    team_ids TEXT,
    objective_info TEXT,
    constant VARCHAR(255),
    classification_xml VARCHAR(500),
    organizer_coefficient DOUBLE PRECISION,
    race_type_id INTEGER,
    is_national_tour BOOLEAN,
    iconic_region_id INTEGER,
    comfort INTEGER,
    is_aso BOOLEAN,
    current_variant VARCHAR(500),
    prize INTEGER,
    is_selected BOOLEAN,
    leap_year_offset INTEGER,
    is_deleted BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX ix_race_country_id ON race (country_id);
CREATE INDEX ix_race_race_class_id ON race (race_class_id);

CREATE TABLE stage (
    id SERIAL PRIMARY KEY,
    race_id INTEGER,
    day INTEGER,
    month INTEGER,
    computed_date INTEGER,
    stage_number INTEGER,
    is_selected BOOLEAN,
    constant VARCHAR(255),
    variant VARCHAR(500),
    is_deleted BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX ix_stage_race_id ON stage (race_id);

COMMIT;
