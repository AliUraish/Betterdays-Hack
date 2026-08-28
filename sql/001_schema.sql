-- Better Days: "find your perfect SF neighborhood" — analytics schema. Works on local CH and ClickHouse Cloud.
CREATE DATABASE IF NOT EXISTS better_days;

-- 117 SF Find Neighborhood polygons. Geometry parsed from WKT once, at insert time.
CREATE TABLE IF NOT EXISTS better_days.neighborhoods
(
    id            UInt32,
    name          String,
    link          String,
    residential   Bool,
    centroid_lat  Float64,
    centroid_lon  Float64,
    wkt           String,
    poly          MultiPolygon MATERIALIZED readWKTMultiPolygon(wkt),
    area_km2      Float64      MATERIALIZED arraySum(p -> polygonAreaSpherical(p), poly) * 6371.0 * 6371.0
)
ENGINE = MergeTree ORDER BY id;

-- Polygon dictionary: point -> neighborhood name. Used to fill the 3% of cases with a blank neighborhood.
CREATE DICTIONARY IF NOT EXISTS better_days.nbhd_dict
(
    poly Array(Array(Array(Tuple(Float64, Float64)))),
    name String
)
PRIMARY KEY poly
SOURCE(CLICKHOUSE(QUERY 'SELECT poly, name FROM better_days.neighborhoods'))
LAYOUT(POLYGON(STORE_POLYGON_KEY_COLUMN 1))
LIFETIME(0);

-- Staging: exact shape of data/cases.csv.gz
CREATE TABLE IF NOT EXISTS better_days.cases_stage
(
    case_id UInt64, opened DateTime, closed Nullable(DateTime), status String, agency String,
    category String, request_type String, request_details String, address String,
    supervisor_district UInt8, neighborhood String, analysis_nbhd String, police_district String,
    lat Float64, lon Float64, source String
)
ENGINE = MergeTree ORDER BY case_id;

-- The fact table.
CREATE TABLE IF NOT EXISTS better_days.cases
(
    case_id              UInt64,
    opened               DateTime,
    closed               Nullable(DateTime),
    status               LowCardinality(String),
    agency               LowCardinality(String),
    category             LowCardinality(String),
    request_type         LowCardinality(String),
    request_details      String,
    address              String,
    supervisor_district  UInt8,
    neighborhood         LowCardinality(String),   -- SF Find Neighborhoods name; joins neighborhoods.name
    analysis_nbhd        LowCardinality(String),
    police_district      LowCardinality(String),
    lat                  Float64,
    lon                  Float64,
    source               LowCardinality(String),
    dimension            LowCardinality(String),   -- livability dimension (see 002_load.sql CASE)
    resolution_hours     Nullable(Float32) MATERIALIZED (closed - opened) / 3600,
    geohash6             String            MATERIALIZED geohashEncode(lon, lat, 6),
    month                Date              MATERIALIZED toStartOfMonth(opened)
)
ENGINE = MergeTree
PARTITION BY toYear(opened)
ORDER BY (neighborhood, dimension, opened);

-- Rollup: neighborhood x dimension x month, incrementally maintained.
CREATE TABLE IF NOT EXISTS better_days.nbhd_dim_month
(
    neighborhood   LowCardinality(String),
    dimension      LowCardinality(String),
    month          Date,
    n              AggregateFunction(count),
    n_closed       AggregateFunction(count),
    res_hours_q    AggregateFunction(quantiles(0.5, 0.9), Float32)
)
ENGINE = AggregatingMergeTree ORDER BY (neighborhood, dimension, month);

CREATE MATERIALIZED VIEW IF NOT EXISTS better_days.mv_nbhd_dim_month TO better_days.nbhd_dim_month AS
SELECT
    neighborhood, dimension, month,
    countState() AS n,
    countStateIf(closed IS NOT NULL) AS n_closed,
    quantilesStateIf(0.5, 0.9)(resolution_hours, resolution_hours IS NOT NULL) AS res_hours_q
FROM better_days.cases
GROUP BY neighborhood, dimension, month;
