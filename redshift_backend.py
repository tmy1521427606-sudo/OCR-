from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence


RULE_VERSION = "knime-db-rules-v1"
IDENTITY_SEPARATOR = "\x1f"

OBJECTS = {
    "goods": "d_platform_goods",
    "monthly": "mv_com_goods_statistics_monthly_v2_internal_ssv4",
    "attributes": "d_platform_goods_attributes",
    "backup": "new_infinitus_attribute",
}

CORE_OBJECTS = {"goods", "monthly", "attributes"}
OPTIONAL_COLUMNS = {("monthly", "rrp")}
MONTHLY_QUERY_TIMEOUT_MS = 300_000
ATTRIBUTE_QUERY_TIMEOUT_MS = 300_000

REQUIRED_COLUMNS = {
    "goods": {
        "platform_goods_id",
        "platform_goods_key",
        "platform_goods_name",
        "last_upd_dt",
        "current_price",
        "original_price",
    },
    "monthly": {
        "platform_goods_id",
        "platform_goods_key",
        "platform_goods_name",
        "month",
        "lowest_promo_price",
        "avg_promo_price",
        "avg_price_m",
        "rrp",
    },
    "attributes": {
        "platform_goods_key",
        "attribute_name",
        "attribute_value",
        "is_active",
        "last_upd_dt",
        "effective_from",
        "job_id",
    },
    "backup": {"item_id", "item_name", "props_name", "props_value", "month"},
}

PLATFORM_COLUMN_CANDIDATES = (
    "platform_key",
    "platform",
    "platform_code",
    "platform_id",
    "platform_name",
    "platform_type",
)

PLATFORM_SCOPE_VALUES = {
    "jd": {
        "platform_key": ("1", "16"),
    },
}

PLATFORM_SOURCE_LABELS = {
    "jd": {
        "1": "京东（主平台，platform_key=1）",
        "16": "京东全球购（补充平台，platform_key=16）",
    },
}


class RedshiftEnrichmentError(RuntimeError):
    pass


class RedshiftSchemaError(RedshiftEnrichmentError):
    pass


def identity_key(platform: Any, product_id: Any) -> str:
    platform_text = str(platform).strip()
    product_text = str(product_id).strip()
    if not platform_text or not product_text:
        raise ValueError("platform and product_id are required")
    if IDENTITY_SEPARATOR in platform_text or IDENTITY_SEPARATOR in product_text:
        raise ValueError("identity contains the reserved separator")
    if len(platform_text) > 128 or len(product_text) > 64:
        raise ValueError("platform or product_id is too long")
    return f"{platform_text}{IDENTITY_SEPARATOR}{product_text}"


def _normalize_identities(identities: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    if not identities:
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in identities:
        if not isinstance(item, dict):
            raise ValueError("each identity must be a dict")
        platform = str(item.get("platform", "")).strip()
        product_id = str(item.get("product_id", "")).strip()
        identity_key(platform, product_id)
        normalized_key = (platform.casefold(), product_id)
        if normalized_key in seen:
            raise ValueError(f"duplicate identity: {platform}/{product_id}")
        seen.add(normalized_key)
        normalized.append({"platform": platform, "product_id": product_id})
    return normalized


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _positive(value: Any) -> Decimal | None:
    result = _decimal(value)
    return result if result is not None and result > 0 else None


def _catalog_rows(connection: Any) -> list[dict[str, Any]]:
    names = list(OBJECTS.values())
    placeholders = ",".join(["%s"] * len(names))
    query = f"""
        SELECT table_schema, table_name, column_name, data_type, ordinal_position
        FROM information_schema.columns
        WHERE LOWER(table_name) IN ({placeholders})
          AND table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY table_schema, table_name, ordinal_position
    """
    with connection.cursor() as cursor:
        cursor.execute(query, names)
        columns = [description.name for description in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    found = {str(row["table_name"]).casefold() for row in rows}
    if {OBJECTS[name] for name in CORE_OBJECTS} <= found:
        return rows

    # Redshift late-binding views are not always exposed by information_schema.
    fallback = f"""
        SELECT schema_name AS table_schema, table_name, column_name,
               data_type, ordinal_position
        FROM svv_columns
        WHERE LOWER(table_name) IN ({placeholders})
        ORDER BY schema_name, table_name, ordinal_position
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(fallback, names)
            columns = [description.name for description in cursor.description]
            extra = [dict(zip(columns, row)) for row in cursor.fetchall()]
    except Exception:
        connection.rollback()
        extra = []

    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in [*rows, *extra]:
        key = (
            str(row["table_schema"]).casefold(),
            str(row["table_name"]).casefold(),
            str(row["column_name"]).casefold(),
        )
        unique[key] = row
    return list(unique.values())


def _resolve_relations(
    connection: Any, requested_schema: str | None
) -> tuple[str, dict[str, dict[str, Any]]]:
    rows = _catalog_rows(connection)
    expected_names = {OBJECTS[name] for name in CORE_OBJECTS}
    schemas: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    actual_schema_names: dict[str, str] = {}
    for row in rows:
        schema_name = str(row["table_schema"])
        table_name = str(row["table_name"])
        schema_folded = schema_name.casefold()
        table_folded = table_name.casefold()
        if table_folded not in expected_names:
            continue
        actual_schema_names[schema_folded] = schema_name
        schemas[schema_folded][table_folded].append(row)

    if requested_schema:
        candidates = [requested_schema.casefold()]
    else:
        candidates = [
            schema_name
            for schema_name, tables in schemas.items()
            if expected_names <= set(tables)
        ]
        if len(candidates) > 1:
            actual = [actual_schema_names[item] for item in sorted(candidates)]
            raise RedshiftSchemaError(
                "multiple schemas contain all four Redshift objects; pass schema=: "
                + ", ".join(actual)
            )

    if len(candidates) != 1 or candidates[0] not in schemas:
        available = sorted(actual_schema_names.values())
        raise RedshiftSchemaError(
            "could not resolve one schema containing all four objects"
            + (f"; available schemas: {', '.join(available)}" if available else "")
        )

    schema_folded = candidates[0]
    actual_schema = actual_schema_names[schema_folded]
    tables = schemas[schema_folded]
    missing_tables = sorted(expected_names - set(tables))
    if missing_tables:
        raise RedshiftSchemaError(
            f"schema {actual_schema!r} is missing: {', '.join(missing_tables)}"
        )

    relations: dict[str, dict[str, Any]] = {}
    for logical_name in CORE_OBJECTS:
        expected_table = OBJECTS[logical_name]
        table_rows = tables[expected_table]
        actual_table = str(table_rows[0]["table_name"])
        columns = {
            str(row["column_name"]).casefold(): str(row["column_name"])
            for row in table_rows
        }
        types = {
            str(row["column_name"]).casefold(): str(row.get("data_type") or "")
            for row in table_rows
        }
        required_columns = REQUIRED_COLUMNS[logical_name] - {
            column_name
            for object_name, column_name in OPTIONAL_COLUMNS
            if object_name == logical_name
        }
        missing_columns = sorted(required_columns - set(columns))
        if missing_columns:
            raise RedshiftSchemaError(
                f"{actual_schema}.{actual_table} is missing columns: "
                + ", ".join(missing_columns)
            )
        relations[logical_name] = {
            "schema": actual_schema,
            "table": actual_table,
            "columns": columns,
            "types": types,
        }

    backup_table = OBJECTS["backup"]
    if backup_table in tables:
        table_rows = tables[backup_table]
        actual_table = str(table_rows[0]["table_name"])
        columns = {
            str(row["column_name"]).casefold(): str(row["column_name"])
            for row in table_rows
        }
        missing_columns = sorted(REQUIRED_COLUMNS["backup"] - set(columns))
        if missing_columns:
            raise RedshiftSchemaError(
                f"{actual_schema}.{actual_table} is missing columns: "
                + ", ".join(missing_columns)
            )
        relations["backup"] = {
            "schema": actual_schema,
            "table": actual_table,
            "columns": columns,
            "types": {},
            "synthetic": False,
        }
    else:
        relations["backup"] = {
            "schema": actual_schema,
            "table": None,
            "columns": {name: name for name in REQUIRED_COLUMNS["backup"]},
            "types": {},
            "synthetic": True,
        }

    goods_platform = _platform_column(relations["goods"])
    monthly_platform = _platform_column(relations["monthly"])
    if bool(goods_platform) != bool(monthly_platform):
        raise RedshiftSchemaError(
            "goods and monthly must both expose a platform column, or neither may do so"
        )
    return actual_schema, relations


def _platform_column(relation: dict[str, Any]) -> str | None:
    columns = relation["columns"]
    matches = [columns[candidate] for candidate in PLATFORM_COLUMN_CANDIDATES if candidate in columns]
    if len(matches) > 1:
        raise RedshiftSchemaError(
            f"{relation['schema']}.{relation['table']} has multiple possible platform columns: "
            + ", ".join(matches)
        )
    return matches[0] if matches else None


def _build_query(
    sql: Any,
    relations: dict[str, dict[str, Any]],
    identities: Sequence[dict[str, str]],
) -> tuple[Any, list[Any]]:
    def relation(name: str) -> Any:
        item = relations[name]
        if item.get("synthetic"):
            return sql.SQL(
                "(SELECT CAST(NULL AS VARCHAR(64)) AS item_id, "
                "CAST(NULL AS VARCHAR(1)) AS item_name, "
                "CAST(NULL AS VARCHAR(1)) AS props_name, "
                "CAST(NULL AS VARCHAR(1)) AS props_value, "
                "CAST(NULL AS VARCHAR(16)) AS month WHERE FALSE)"
            )
        return sql.Identifier(item["schema"], item["table"])

    def column(name: str, logical_column: str, alias: str) -> Any:
        actual = relations[name]["columns"][logical_column]
        return sql.SQL("{}.{}").format(sql.SQL(alias), sql.Identifier(actual))

    def optional_column(
        name: str, logical_column: str, alias: str, null_type: str
    ) -> Any:
        if logical_column not in relations[name]["columns"]:
            return sql.SQL(f"CAST(NULL AS {null_type})")
        return column(name, logical_column, alias)

    def platform_expression(name: str, alias: str) -> Any:
        actual = _platform_column(relations[name])
        if not actual:
            return sql.SQL("CAST(NULL AS VARCHAR(128))")
        return sql.SQL("NULLIF(LOWER(TRIM(CAST({}.{} AS VARCHAR(128)))), '')").format(
            sql.SQL(alias), sql.Identifier(actual)
        )

    def scope_predicate(name: str) -> Any:
        return (
            sql.SQL("source_platform = platform_norm")
            if _platform_column(relations[name])
            else sql.SQL("TRUE")
        )

    input_rows = sql.SQL("\n    UNION ALL\n    ").join(
        sql.SQL(
            "SELECT CAST(%s AS INTEGER) AS input_ord, "
            "CAST(%s AS VARCHAR(128)) AS input_platform, "
            "CAST(%s AS VARCHAR(64)) AS product_id"
        )
        for _ in identities
    )
    id_placeholders = {
        name: sql.SQL(",").join(sql.Placeholder() for _ in identities)
        for name in ("goods", "monthly", "backup")
    }
    params: list[Any] = []
    for ordinal, item in enumerate(identities):
        params.extend((ordinal, item["platform"], item["product_id"]))
    for _ in ("goods", "monthly", "backup"):
        params.extend(item["product_id"] for item in identities)

    core_has_platform = int(bool(_platform_column(relations["goods"])))
    backup_has_platform = int(bool(_platform_column(relations["backup"])))

    query = sql.SQL(
        """
WITH
input_raw AS (
    {input_rows}
),
input_ids AS (
    SELECT input_ord, input_platform,
           LOWER(TRIM(input_platform)) AS platform_norm,
           product_id
    FROM input_raw
),
input_id_stats AS (
    SELECT product_id, COUNT(DISTINCT platform_norm) AS input_platform_count
    FROM input_ids
    GROUP BY product_id
),
goods_all AS (
    SELECT i.input_ord, i.input_platform, i.platform_norm, i.product_id,
           {g_platform} AS source_platform,
           {g_key} AS platform_goods_key,
           NULLIF(TRIM({g_name}), '') AS product_name,
           {g_upd} AS source_time,
           {g_current} AS current_price,
           {g_original} AS original_price
    FROM input_ids i
    JOIN {g_relation} g ON {g_id} = i.product_id
    WHERE {g_id} IN ({g_ids})
),
monthly_all AS (
    SELECT i.input_ord, i.input_platform, i.platform_norm, i.product_id,
           {m_platform} AS source_platform,
           {m_key} AS platform_goods_key,
           NULLIF(TRIM({m_name}), '') AS product_name,
           {m_month} AS source_time,
           {m_lowest} AS lowest_promo_price,
           {m_avg_promo} AS avg_promo_price,
           {m_avg_price} AS avg_price_m,
           {m_rrp} AS rrp
    FROM input_ids i
    JOIN {m_relation} m ON {m_id} = i.product_id
    WHERE {m_id} IN ({m_ids})
),
new_all AS (
    SELECT i.input_ord, i.input_platform, i.platform_norm, i.product_id,
           {n_platform} AS source_platform,
           NULLIF(TRIM({n_name}), '') AS product_name,
           {n_prop_name} AS props_name,
           {n_prop_value} AS props_value,
           {n_month} AS source_time
    FROM input_ids i
    JOIN {n_relation} n ON {n_id} = i.product_id
    WHERE {n_id} IN ({n_ids})
),
goods_scoped AS (
    SELECT * FROM goods_all WHERE {g_scope}
),
monthly_scoped AS (
    SELECT * FROM monthly_all WHERE {m_scope}
),
new_scoped AS (
    SELECT * FROM new_all WHERE {n_scope}
),
core_all AS (
    SELECT input_ord FROM goods_all
    UNION ALL
    SELECT input_ord FROM monthly_all
),
core_scoped AS (
    SELECT input_ord FROM goods_scoped
    UNION ALL
    SELECT input_ord FROM monthly_scoped
),
core_all_counts AS (
    SELECT input_ord, COUNT(*) AS candidate_row_count
    FROM core_all GROUP BY input_ord
),
core_scoped_counts AS (
    SELECT input_ord, COUNT(*) AS scoped_row_count
    FROM core_scoped GROUP BY input_ord
),
identity_guard AS (
    SELECT i.input_ord,
           COALESCE(a.candidate_row_count, 0) AS candidate_row_count,
           COALESCE(s.scoped_row_count, 0) AS scoped_row_count,
           st.input_platform_count,
           CASE
             WHEN {core_has_platform} = 0 AND st.input_platform_count > 1
               THEN 'ambiguous'
             WHEN {core_has_platform} = 1 AND COALESCE(s.scoped_row_count, 0) > 0
               THEN 'exact'
             WHEN {core_has_platform} = 1 AND COALESCE(a.candidate_row_count, 0) > 0
               THEN 'not_matched'
             WHEN {core_has_platform} = 1 THEN 'id_not_found'
             ELSE 'unscoped_unique_input'
           END AS platform_status,
           CASE
             WHEN {core_has_platform} = 0 AND st.input_platform_count > 1 THEN 1
             WHEN {core_has_platform} = 1
                  AND COALESCE(a.candidate_row_count, 0) > 0
                  AND COALESCE(s.scoped_row_count, 0) = 0 THEN 1
             ELSE 0
           END AS identity_blocked
    FROM input_ids i
    JOIN input_id_stats st ON st.product_id = i.product_id
    LEFT JOIN core_all_counts a ON a.input_ord = i.input_ord
    LEFT JOIN core_scoped_counts s ON s.input_ord = i.input_ord
),
goods_key_ranked AS (
    SELECT g.*,
           DENSE_RANK() OVER (
             PARTITION BY input_ord ORDER BY source_time DESC NULLS LAST
           ) AS latest_rank
    FROM goods_scoped g
    WHERE platform_goods_key IS NOT NULL
),
goods_key_variants AS (
    SELECT DISTINCT input_ord, platform_goods_key, source_time
    FROM goods_key_ranked WHERE latest_rank = 1
),
goods_key_choices AS (
    SELECT v.*,
           COUNT(*) OVER (PARTITION BY input_ord) AS variant_count,
           ROW_NUMBER() OVER (
             PARTITION BY input_ord ORDER BY CAST(platform_goods_key AS VARCHAR(256))
           ) AS choice_rank
    FROM goods_key_variants v
),
goods_key AS (
    SELECT * FROM goods_key_choices WHERE choice_rank = 1
),
monthly_key_ranked AS (
    SELECT m.*,
           DENSE_RANK() OVER (
             PARTITION BY input_ord ORDER BY source_time DESC NULLS LAST
           ) AS latest_rank
    FROM monthly_scoped m
    WHERE platform_goods_key IS NOT NULL
),
monthly_key_variants AS (
    SELECT DISTINCT input_ord, platform_goods_key, source_time
    FROM monthly_key_ranked WHERE latest_rank = 1
),
monthly_key_choices AS (
    SELECT v.*,
           COUNT(*) OVER (PARTITION BY input_ord) AS variant_count,
           ROW_NUMBER() OVER (
             PARTITION BY input_ord ORDER BY CAST(platform_goods_key AS VARCHAR(256))
           ) AS choice_rank
    FROM monthly_key_variants v
),
monthly_key AS (
    SELECT * FROM monthly_key_choices WHERE choice_rank = 1
),
key_resolution AS (
    SELECT i.input_ord,
           CASE
             WHEN guard.identity_blocked = 1 THEN NULL
             WHEN g.variant_count > 1 THEN NULL
             WHEN {core_has_platform} = 0
                  AND g.variant_count = 1 AND m.variant_count = 1
                  AND CAST(g.platform_goods_key AS VARCHAR(256)) <>
                      CAST(m.platform_goods_key AS VARCHAR(256)) THEN NULL
             WHEN g.platform_goods_key IS NOT NULL THEN g.platform_goods_key
             WHEN m.variant_count > 1 THEN NULL
             ELSE m.platform_goods_key
           END AS resolved_key,
           CASE
             WHEN guard.identity_blocked = 1 THEN 'identity_blocked'
             WHEN g.variant_count > 1 THEN 'ambiguous'
             WHEN {core_has_platform} = 0
                  AND g.variant_count = 1 AND m.variant_count = 1
                  AND CAST(g.platform_goods_key AS VARCHAR(256)) <>
                      CAST(m.platform_goods_key AS VARCHAR(256)) THEN 'ambiguous'
             WHEN g.platform_goods_key IS NOT NULL THEN 'ok'
             WHEN m.variant_count > 1 THEN 'ambiguous'
             WHEN m.platform_goods_key IS NOT NULL THEN 'ok'
             ELSE 'missing'
           END AS key_status,
           CASE
             WHEN guard.identity_blocked = 0 AND g.variant_count = 1
                  AND NOT ({core_has_platform} = 0
                           AND COALESCE(m.variant_count, 0) = 1
                           AND CAST(g.platform_goods_key AS VARCHAR(256)) <>
                               CAST(m.platform_goods_key AS VARCHAR(256)))
               THEN 'd_platform_goods'
             WHEN guard.identity_blocked = 0 AND g.input_ord IS NULL
                  AND m.variant_count = 1 THEN
               'mv_com_goods_statistics_monthly_v2_internal_ssv4'
             ELSE NULL
           END AS key_source,
           CASE
             WHEN guard.identity_blocked = 0 AND g.variant_count = 1
                  AND NOT ({core_has_platform} = 0
                           AND COALESCE(m.variant_count, 0) = 1
                           AND CAST(g.platform_goods_key AS VARCHAR(256)) <>
                               CAST(m.platform_goods_key AS VARCHAR(256)))
               THEN CAST(g.source_time AS VARCHAR(64))
             WHEN guard.identity_blocked = 0 AND g.input_ord IS NULL
                  AND m.variant_count = 1 THEN CAST(m.source_time AS VARCHAR(64))
             ELSE NULL
           END AS key_source_time,
           g.platform_goods_key AS goods_key,
           m.platform_goods_key AS monthly_key,
           COALESCE(g.variant_count, 0) AS goods_key_variant_count,
           COALESCE(m.variant_count, 0) AS monthly_key_variant_count,
           CASE
             WHEN g.variant_count = 1 AND m.variant_count = 1
                  AND CAST(g.platform_goods_key AS VARCHAR(256)) <>
                      CAST(m.platform_goods_key AS VARCHAR(256)) THEN 1
             ELSE 0
           END AS key_source_conflict
    FROM input_ids i
    JOIN identity_guard guard ON guard.input_ord = i.input_ord
    LEFT JOIN goods_key g ON g.input_ord = i.input_ord
    LEFT JOIN monthly_key m ON m.input_ord = i.input_ord
),
main_attribute_valid AS (
    SELECT i.input_ord,
           TRIM({a_name}) AS attribute_name,
           TRIM({a_value}) AS attribute_value,
           {a_upd} AS source_time,
           {a_effective} AS effective_from,
           {a_job} AS job_id
    FROM input_ids i
    JOIN key_resolution k ON k.input_ord = i.input_ord AND k.key_status = 'ok'
    JOIN {a_relation} a ON {a_key} = k.resolved_key
    WHERE {a_active} = 1
      AND NULLIF(TRIM({a_name}), '') IS NOT NULL
      AND NULLIF(TRIM({a_value}), '') IS NOT NULL
),
main_attribute_ranked AS (
    SELECT v.*,
           ROW_NUMBER() OVER (
             PARTITION BY input_ord, attribute_name
             ORDER BY source_time DESC NULLS LAST,
                      effective_from DESC NULLS LAST,
                      job_id DESC NULLS LAST,
                      attribute_value ASC
           ) AS attribute_rank
    FROM main_attribute_valid v
),
main_attributes AS (
    SELECT input_ord, attribute_name, attribute_value,
           'd_platform_goods_attributes' AS attribute_source,
           CAST(source_time AS VARCHAR(64)) AS attribute_source_time,
           1 AS attribute_source_priority
    FROM main_attribute_ranked WHERE attribute_rank = 1
),
main_attribute_exists AS (
    SELECT DISTINCT input_ord FROM main_attribute_valid
),
new_safe AS (
    SELECT n.*
    FROM new_scoped n
    JOIN identity_guard guard ON guard.input_ord = n.input_ord
    JOIN input_id_stats st ON st.product_id = n.product_id
    WHERE guard.identity_blocked = 0
      AND ({backup_has_platform} = 1 OR st.input_platform_count = 1)
),
fallback_ranked AS (
    SELECT n.*,
           DENSE_RANK() OVER (
             PARTITION BY n.input_ord ORDER BY n.source_time DESC NULLS LAST
           ) AS month_rank
    FROM new_safe n
    JOIN key_resolution k ON k.input_ord = n.input_ord
    WHERE k.key_status <> 'ambiguous' AND k.key_status <> 'identity_blocked'
),
fallback_attributes AS (
    SELECT DISTINCT f.input_ord,
           TRIM(f.props_name) AS attribute_name,
           TRIM(f.props_value) AS attribute_value,
           'new_infinitus_attribute' AS attribute_source,
           CAST(f.source_time AS VARCHAR(64)) AS attribute_source_time,
           2 AS attribute_source_priority
    FROM fallback_ranked f
    LEFT JOIN main_attribute_exists e ON e.input_ord = f.input_ord
    WHERE e.input_ord IS NULL
      AND f.month_rank = 1
      AND NULLIF(TRIM(f.props_name), '') IS NOT NULL
      AND NULLIF(TRIM(f.props_value), '') IS NOT NULL
),
selected_attributes AS (
    SELECT * FROM main_attributes
    UNION ALL
    SELECT * FROM fallback_attributes
),
goods_name_ranked AS (
    SELECT g.*,
           ROW_NUMBER() OVER (
             PARTITION BY g.input_ord
             ORDER BY g.source_time DESC NULLS LAST,
                      g.product_name ASC,
                      CAST(g.platform_goods_key AS VARCHAR(256)) ASC
           ) AS name_rank
    FROM goods_scoped g
    JOIN identity_guard guard ON guard.input_ord = g.input_ord
    WHERE guard.identity_blocked = 0 AND g.product_name IS NOT NULL
),
monthly_name_ranked AS (
    SELECT m.*,
           ROW_NUMBER() OVER (
             PARTITION BY m.input_ord
             ORDER BY m.source_time DESC NULLS LAST,
                      m.product_name ASC,
                      CAST(m.platform_goods_key AS VARCHAR(256)) ASC
           ) AS name_rank
    FROM monthly_scoped m
    JOIN identity_guard guard ON guard.input_ord = m.input_ord
    WHERE guard.identity_blocked = 0 AND m.product_name IS NOT NULL
),
new_name_ranked AS (
    SELECT n.*,
           ROW_NUMBER() OVER (
             PARTITION BY n.input_ord
             ORDER BY n.source_time DESC NULLS LAST, n.product_name ASC
           ) AS name_rank
    FROM new_safe n
    WHERE n.product_name IS NOT NULL
),
name_candidates AS (
    SELECT input_ord, product_name, 'd_platform_goods' AS name_source,
           CAST(source_time AS VARCHAR(64)) AS name_source_time, 1 AS source_priority
    FROM goods_name_ranked WHERE name_rank = 1
    UNION ALL
    SELECT input_ord, product_name,
           'mv_com_goods_statistics_monthly_v2_internal_ssv4',
           CAST(source_time AS VARCHAR(64)), 2
    FROM monthly_name_ranked WHERE name_rank = 1
    UNION ALL
    SELECT input_ord, product_name, 'new_infinitus_attribute',
           CAST(source_time AS VARCHAR(64)), 3
    FROM new_name_ranked WHERE name_rank = 1
),
best_name AS (
    SELECT c.*,
           ROW_NUMBER() OVER (
             PARTITION BY input_ord ORDER BY source_priority
           ) AS best_rank
    FROM name_candidates c
),
monthly_price_rows AS (
    SELECT m.input_ord, m.source_time,
           CASE
             WHEN m.lowest_promo_price > 0 THEN m.lowest_promo_price
             WHEN m.avg_promo_price > 0 THEN m.avg_promo_price
             WHEN m.avg_price_m > 0 THEN m.avg_price_m
             WHEN m.rrp > 0 THEN m.rrp
             ELSE NULL
           END AS price,
           CASE
             WHEN m.lowest_promo_price > 0 THEN 'lowest_promo_price'
             WHEN m.avg_promo_price > 0 THEN 'avg_promo_price'
             WHEN m.avg_price_m > 0 THEN 'avg_price_m'
             WHEN m.rrp > 0 THEN 'rrp'
             ELSE NULL
           END AS source_column
    FROM monthly_scoped m
    JOIN identity_guard guard ON guard.input_ord = m.input_ord
    WHERE guard.identity_blocked = 0
      AND (m.lowest_promo_price > 0 OR m.avg_promo_price > 0
           OR m.avg_price_m > 0 OR m.rrp > 0)
),
monthly_price_ranked AS (
    SELECT p.*,
           DENSE_RANK() OVER (
             PARTITION BY input_ord ORDER BY source_time DESC NULLS LAST
           ) AS latest_rank
    FROM monthly_price_rows p
),
monthly_price_variants AS (
    SELECT DISTINCT input_ord, source_time, source_column, price
    FROM monthly_price_ranked WHERE latest_rank = 1
),
monthly_price_choices AS (
    SELECT v.*,
           COUNT(*) OVER (PARTITION BY input_ord) AS variant_count,
           ROW_NUMBER() OVER (
             PARTITION BY input_ord ORDER BY source_column, price
           ) AS choice_rank
    FROM monthly_price_variants v
),
monthly_price AS (
    SELECT * FROM monthly_price_choices WHERE choice_rank = 1
),
goods_price_rows AS (
    SELECT g.input_ord, g.source_time,
           CASE
             WHEN g.current_price > 0 THEN g.current_price
             WHEN g.original_price > 0 THEN g.original_price
             ELSE NULL
           END AS price,
           CASE
             WHEN g.current_price > 0 THEN 'current_price'
             WHEN g.original_price > 0 THEN 'original_price'
             ELSE NULL
           END AS source_column
    FROM goods_scoped g
    JOIN identity_guard guard ON guard.input_ord = g.input_ord
    WHERE guard.identity_blocked = 0
      AND (g.current_price > 0 OR g.original_price > 0)
),
goods_price_ranked AS (
    SELECT p.*,
           DENSE_RANK() OVER (
             PARTITION BY input_ord ORDER BY source_time DESC NULLS LAST
           ) AS latest_rank
    FROM goods_price_rows p
),
goods_price_variants AS (
    SELECT DISTINCT input_ord, source_time, source_column, price
    FROM goods_price_ranked WHERE latest_rank = 1
),
goods_price_choices AS (
    SELECT v.*,
           COUNT(*) OVER (PARTITION BY input_ord) AS variant_count,
           ROW_NUMBER() OVER (
             PARTITION BY input_ord ORDER BY source_column, price
           ) AS choice_rank
    FROM goods_price_variants v
),
goods_price AS (
    SELECT * FROM goods_price_choices WHERE choice_rank = 1
),
price_resolution AS (
    SELECT i.input_ord,
           CASE
             WHEN guard.identity_blocked = 1 THEN 'identity_blocked'
             WHEN mp.variant_count > 1 THEN 'ambiguous'
             WHEN mp.variant_count = 1 THEN 'ok'
             WHEN gp.variant_count > 1 THEN 'ambiguous'
             WHEN gp.variant_count = 1 THEN 'ok'
             ELSE 'missing'
           END AS price_status,
           CASE
             WHEN guard.identity_blocked = 0 AND mp.variant_count = 1 THEN mp.price
             WHEN guard.identity_blocked = 0 AND mp.input_ord IS NULL
                  AND gp.variant_count = 1 THEN gp.price
             ELSE NULL
           END AS price,
           CASE
             WHEN guard.identity_blocked = 0 AND mp.variant_count = 1
               THEN 'mv_com_goods_statistics_monthly_v2_internal_ssv4'
             WHEN guard.identity_blocked = 0 AND mp.input_ord IS NULL
                  AND gp.variant_count = 1 THEN 'd_platform_goods'
             ELSE NULL
           END AS price_source_table,
           CASE
             WHEN guard.identity_blocked = 0 AND mp.variant_count = 1
               THEN mp.source_column
             WHEN guard.identity_blocked = 0 AND mp.input_ord IS NULL
                  AND gp.variant_count = 1 THEN gp.source_column
             ELSE NULL
           END AS price_source_column,
           CASE
             WHEN guard.identity_blocked = 0 AND mp.variant_count = 1
               THEN CAST(mp.source_time AS VARCHAR(64))
             WHEN guard.identity_blocked = 0 AND mp.input_ord IS NULL
                  AND gp.variant_count = 1 THEN CAST(gp.source_time AS VARCHAR(64))
             ELSE NULL
           END AS price_source_time,
           COALESCE(mp.variant_count, 0) AS monthly_price_variant_count,
           COALESCE(gp.variant_count, 0) AS goods_price_variant_count
    FROM input_ids i
    JOIN identity_guard guard ON guard.input_ord = i.input_ord
    LEFT JOIN monthly_price mp ON mp.input_ord = i.input_ord
    LEFT JOIN goods_price gp ON gp.input_ord = i.input_ord
)
SELECT i.input_ord, i.input_platform, i.product_id,
       guard.platform_status, guard.candidate_row_count, guard.scoped_row_count,
       k.resolved_key, k.key_status, k.key_source, k.key_source_time,
       k.goods_key, k.monthly_key, k.goods_key_variant_count,
       k.monthly_key_variant_count, k.key_source_conflict,
       bn.product_name, bn.name_source, bn.name_source_time,
       p.price_status, p.price, p.price_source_table, p.price_source_column,
       p.price_source_time, p.monthly_price_variant_count,
       p.goods_price_variant_count,
       a.attribute_name, a.attribute_value, a.attribute_source,
       a.attribute_source_time, a.attribute_source_priority,
       CURRENT_TIMESTAMP AS snapshot_at
FROM input_ids i
JOIN identity_guard guard ON guard.input_ord = i.input_ord
JOIN key_resolution k ON k.input_ord = i.input_ord
JOIN price_resolution p ON p.input_ord = i.input_ord
LEFT JOIN best_name bn ON bn.input_ord = i.input_ord AND bn.best_rank = 1
LEFT JOIN selected_attributes a ON a.input_ord = i.input_ord
ORDER BY i.input_ord,
         COALESCE(a.attribute_source_priority, 99),
         a.attribute_name,
         a.attribute_value
"""
    ).format(
        input_rows=input_rows,
        g_relation=relation("goods"),
        m_relation=relation("monthly"),
        a_relation=relation("attributes"),
        n_relation=relation("backup"),
        g_ids=id_placeholders["goods"],
        m_ids=id_placeholders["monthly"],
        n_ids=id_placeholders["backup"],
        g_platform=platform_expression("goods", "g"),
        m_platform=platform_expression("monthly", "m"),
        n_platform=platform_expression("backup", "n"),
        g_scope=scope_predicate("goods"),
        m_scope=scope_predicate("monthly"),
        n_scope=scope_predicate("backup"),
        core_has_platform=sql.Literal(core_has_platform),
        backup_has_platform=sql.Literal(backup_has_platform),
        g_id=column("goods", "platform_goods_id", "g"),
        g_key=column("goods", "platform_goods_key", "g"),
        g_name=column("goods", "platform_goods_name", "g"),
        g_upd=column("goods", "last_upd_dt", "g"),
        g_current=column("goods", "current_price", "g"),
        g_original=column("goods", "original_price", "g"),
        m_id=column("monthly", "platform_goods_id", "m"),
        m_key=column("monthly", "platform_goods_key", "m"),
        m_name=column("monthly", "platform_goods_name", "m"),
        m_month=column("monthly", "month", "m"),
        m_lowest=column("monthly", "lowest_promo_price", "m"),
        m_avg_promo=column("monthly", "avg_promo_price", "m"),
        m_avg_price=column("monthly", "avg_price_m", "m"),
        m_rrp=optional_column("monthly", "rrp", "m", "NUMERIC"),
        n_id=column("backup", "item_id", "n"),
        n_name=column("backup", "item_name", "n"),
        n_prop_name=column("backup", "props_name", "n"),
        n_prop_value=column("backup", "props_value", "n"),
        n_month=column("backup", "month", "n"),
        a_key=column("attributes", "platform_goods_key", "a"),
        a_name=column("attributes", "attribute_name", "a"),
        a_value=column("attributes", "attribute_value", "a"),
        a_active=column("attributes", "is_active", "a"),
        a_upd=column("attributes", "last_upd_dt", "a"),
        a_effective=column("attributes", "effective_from", "a"),
        a_job=column("attributes", "job_id", "a"),
    )
    return query, params


def _build_lookup_query(
    sql: Any,
    relation: dict[str, Any],
    logical_columns: Sequence[str],
    filter_column: str,
) -> Any:
    if relation.get("synthetic"):
        raise ValueError("synthetic relations cannot be queried")
    columns = relation["columns"]
    selected = sql.SQL(", ").join(
        sql.Identifier(columns[name]) for name in logical_columns
    )
    return sql.SQL("SELECT {} FROM {} WHERE {} = ANY(%s)").format(
        selected,
        sql.Identifier(relation["schema"], relation["table"]),
        sql.Identifier(columns[filter_column]),
    )


def _issue(code: str, message: str, severity: str = "error") -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def _assemble_result(
    identity: dict[str, str],
    rows: Sequence[dict[str, Any]],
    relation_signature: Any,
) -> dict[str, Any]:
    first = rows[0]
    attributes = [
        {
            "name": row["attribute_name"],
            "value": row["attribute_value"],
            "source_table": row["attribute_source"],
            "source_time": _json_value(row.get("attribute_source_time")),
        }
        for row in rows
        if row.get("attribute_name") is not None
    ]
    attributes.sort(
        key=lambda item: (
            str(item["source_table"]),
            str(item["name"]),
            str(item["value"]),
        )
    )

    platform_status = str(first.get("platform_status") or "unknown")
    key_status = str(first.get("key_status") or "missing")
    price_status = str(first.get("price_status") or "missing")
    issues: list[dict[str, str]] = []
    if platform_status == "ambiguous":
        issues.append(_issue("PLATFORM_AMBIGUOUS", "同一商品ID无法按平台安全隔离"))
    elif platform_status == "not_matched":
        issues.append(_issue("PLATFORM_NOT_MATCHED", "数据库存在该ID，但平台值不匹配"))
    elif platform_status == "id_not_found":
        issues.append(_issue("ID_NOT_FOUND", "商品主表和月度表均未找到该ID"))
    elif platform_status == "unscoped_unique_input":
        issues.append(
            _issue(
                "PLATFORM_UNVERIFIED",
                "数据库对象没有可用平台字段，本次仅按商品ID匹配；跨平台使用前必须确认ID全局唯一或补平台映射",
                "warning",
            )
        )
    if key_status == "ambiguous":
        issues.append(_issue("KEY_AMBIGUOUS", "最新候选包含多个商品key"))
    elif key_status == "missing":
        issues.append(_issue("KEY_NOT_FOUND", "未找到商品key"))
    if first.get("key_source_conflict"):
        issues.append(_issue("KEY_SOURCE_CONFLICT", "主表与月度表商品key不同"))
    if price_status == "ambiguous":
        issues.append(_issue("PRICE_AMBIGUOUS", "最新价格时点包含多个候选，未回退旧来源"))
    elif price_status == "missing":
        issues.append(_issue("PRICE_NOT_FOUND", "月度表和商品主表均无有效价格"))
    if first.get("product_name") is None:
        issues.append(_issue("NAME_NOT_FOUND", "三个名称来源均无有效商品名"))
    if not attributes:
        issues.append(_issue("ATTRIBUTES_NOT_FOUND", "主属性和整组备用属性均为空"))

    result = {
        "identity": {
            "platform": identity["platform"],
            "product_id": identity["product_id"],
            "platform_match_mode": platform_status,
            "candidate_row_count": int(first.get("candidate_row_count") or 0),
            "scoped_row_count": int(first.get("scoped_row_count") or 0),
            "snapshot_at": _json_value(first.get("snapshot_at")),
        },
        "key": {
            "value": first.get("resolved_key"),
            "status": key_status,
            "source_table": first.get("key_source"),
            "source_time": _json_value(first.get("key_source_time")),
            "goods_value": first.get("goods_key"),
            "monthly_value": first.get("monthly_key"),
            "goods_variant_count": int(first.get("goods_key_variant_count") or 0),
            "monthly_variant_count": int(first.get("monthly_key_variant_count") or 0),
            "source_conflict": bool(first.get("key_source_conflict")),
        },
        "name": {
            "value": first.get("product_name"),
            "source_table": first.get("name_source"),
            "source_time": _json_value(first.get("name_source_time")),
        },
        "platform_source": {
            "key": first.get("source_platform_key"),
            "label": first.get("source_platform_label"),
        },
        "attributes": attributes,
        "price": {
            "value": _decimal(first.get("price")),
            "raw_value": _decimal(first.get("price")),
            "status": price_status,
            "source_table": first.get("price_source_table"),
            "source_column": first.get("price_source_column"),
            "source_time": _json_value(first.get("price_source_time")),
            "platform_source": {
                "key": first.get("price_platform_key"),
                "label": first.get("price_platform_label"),
            },
            "monthly_variant_count": int(
                first.get("monthly_price_variant_count") or 0
            ),
            "goods_variant_count": int(first.get("goods_price_variant_count") or 0),
        },
        "review_issues": issues,
    }
    snapshot = {
        "rule_version": RULE_VERSION,
        "relations": relation_signature,
        "identity": identity,
        "key": result["key"],
        "name": result["name"],
        "attributes": result["attributes"],
        "price": result["price"],
    }
    result["db_snapshot_hash"] = _stable_hash(snapshot)
    return result


def enrich_products(
    connection_info: dict[str, Any],
    identities: list[dict[str, Any]],
    schema: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Batch-enrich product identities with small, indexable table lookups."""
    normalized = _normalize_identities(identities)
    if not normalized:
        return {}
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RedshiftEnrichmentError(
            "psycopg 3 is required: python -m pip install 'psycopg[binary]'"
        ) from exc

    product_ids = list(dict.fromkeys(item["product_id"] for item in normalized))
    with psycopg.connect(**dict(connection_info)) as connection:
        actual_schema, relations = _resolve_relations(connection, schema)
        def fetch_rows(
            logical_name: str,
            filter_column: str,
            values: Sequence[Any],
            statement_timeout_ms: int | None = None,
        ) -> list[dict[str, Any]]:
            if not values or relations[logical_name].get("synthetic"):
                return []
            columns = tuple(
                name
                for name in sorted(REQUIRED_COLUMNS[logical_name])
                if name in relations[logical_name]["columns"]
            )
            platform_column = _platform_column(relations[logical_name])
            if platform_column:
                columns = tuple(sorted({*columns, platform_column}))
            query = _build_lookup_query(
                sql, relations[logical_name], columns, filter_column
            )
            target = f"{relations[logical_name]['schema']}.{relations[logical_name]['table']}"
            print(f"PostgreSQL 小范围查询 {target}：{len(values)} 个键")
            started = time.perf_counter()
            try:
                with connection.cursor() as cursor:
                    if statement_timeout_ms is not None:
                        cursor.execute(
                            f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"
                        )
                    cursor.execute(query, (list(values),))
                    names = [description.name for description in cursor.description]
                    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
            except Exception as exc:
                elapsed_seconds = time.perf_counter() - started
                print(f"PostgreSQL 小范围查询失败 {target}：耗时 {elapsed_seconds:.1f} 秒")
                raise RedshiftEnrichmentError(
                    f"{target} 查询失败（耗时 {elapsed_seconds:.1f} 秒）: {exc}"
                ) from exc
            elapsed_seconds = time.perf_counter() - started
            print(
                f"PostgreSQL 小范围查询完成 {target}：{len(rows)} 行，"
                f"耗时 {elapsed_seconds:.1f} 秒"
            )
            return rows

        goods_rows = fetch_rows("goods", "platform_goods_id", product_ids)
        query_warnings: list[dict[str, str]] = []
        monthly_rows = fetch_rows(
            "monthly", "platform_goods_id", product_ids,
            statement_timeout_ms=MONTHLY_QUERY_TIMEOUT_MS,
        )
        key_values = list(
            dict.fromkeys(
                row["platform_goods_key"]
                for row in [*goods_rows, *monthly_rows]
                if row.get("platform_goods_key") is not None
            )
        )
        attribute_rows = fetch_rows(
            "attributes", "platform_goods_key", key_values,
            statement_timeout_ms=ATTRIBUTE_QUERY_TIMEOUT_MS,
        )
        backup_rows = fetch_rows("backup", "item_id", product_ids)

    relation_signature = {
        "schema": actual_schema,
        "objects": {name: item["table"] for name, item in relations.items()},
        "platform_columns": {
            name: _platform_column(item) for name, item in relations.items()
        },
    }
    return resolve_mock_rows(
        normalized,
        goods_rows=goods_rows,
        monthly_rows=monthly_rows,
        attribute_rows=attribute_rows,
        backup_rows=backup_rows,
        relation_signature=relation_signature,
        snapshot_at=datetime.now().astimezone().isoformat(),
        query_warnings=query_warnings,
    )


def _mock_platform_column(rows: Sequence[dict[str, Any]]) -> str | None:
    keys = {str(key).casefold(): str(key) for row in rows for key in row}
    matches = [keys[candidate] for candidate in PLATFORM_COLUMN_CANDIDATES if candidate in keys]
    if len(matches) > 1:
        raise ValueError("mock rows have multiple possible platform columns: " + ", ".join(matches))
    return matches[0] if matches else None


def _platform_scope_values(platform: str, column: str) -> tuple[str, ...] | None:
    platform_norm = platform.casefold().strip()
    platform_scope = PLATFORM_SCOPE_VALUES.get(platform_norm, {})
    if column.casefold() == "platform_key":
        return platform_scope.get("platform_key")
    return (platform_norm,)


def _platform_source_info(platform: str, platform_key: Any) -> dict[str, Any]:
    key = "" if platform_key is None else str(platform_key).strip()
    label = PLATFORM_SOURCE_LABELS.get(platform.casefold().strip(), {}).get(key)
    if label is None:
        label = f"未映射平台（platform_key={key or '-'}）"
    return {"key": platform_key, "label": label}


def _source_platform_key(
    rows: Sequence[dict[str, Any]], key: Any, time_column: str
) -> Any:
    matching = [
        row for row in rows
        if str(row.get("platform_goods_key")) == str(key)
    ]
    latest = _latest_rows(matching, time_column)
    return latest[0].get("platform_key") if latest else None


def _mock_scope(
    rows: Sequence[dict[str, Any]],
    id_column: str,
    product_id: str,
    platform: str,
) -> tuple[list[dict[str, Any]], bool]:
    candidates = [row for row in rows if str(row.get(id_column, "")).strip() == product_id]
    platform_column = _mock_platform_column(rows)
    if not platform_column:
        return candidates, False
    scope_values = _platform_scope_values(platform, platform_column)
    if scope_values is None:
        return candidates, False
    scoped = [
        row
        for row in candidates
        if str(row.get(platform_column, "")).casefold().strip() in scope_values
    ]
    primary = [
        row
        for row in scoped
        if str(row.get(platform_column, "")).casefold().strip() == scope_values[0]
    ]
    return primary or scoped, True


def _time_key(value: Any) -> tuple[int, str]:
    if value is None:
        return (0, "")
    if isinstance(value, (date, datetime)):
        return (1, value.isoformat())
    return (1, str(value))


def _latest_rows(rows: Sequence[dict[str, Any]], time_column: str) -> list[dict[str, Any]]:
    if not rows:
        return []
    latest = max(_time_key(row.get(time_column)) for row in rows)
    return [row for row in rows if _time_key(row.get(time_column)) == latest]


def _mock_key_choice(
    rows: Sequence[dict[str, Any]], time_column: str
) -> tuple[Any, int, Any]:
    latest = _latest_rows(
        [row for row in rows if row.get("platform_goods_key") is not None],
        time_column,
    )
    variants: dict[str, dict[str, Any]] = {}
    for row in latest:
        variants[str(row["platform_goods_key"])] = row
    if not variants:
        return None, 0, None
    row = variants[sorted(variants)[0]]
    return row["platform_goods_key"], len(variants), row.get(time_column)


def _mock_name(rows: Sequence[dict[str, Any]], time_column: str) -> tuple[Any, Any]:
    valid = [
        row
        for row in rows
        if row.get("platform_goods_name") is not None
        and str(row["platform_goods_name"]).strip()
    ]
    latest = _latest_rows(valid, time_column)
    if not latest:
        return None, None
    row = sorted(latest, key=lambda item: str(item["platform_goods_name"]))[0]
    return str(row["platform_goods_name"]).strip(), row.get(time_column)


def _mock_price(
    rows: Sequence[dict[str, Any]],
    time_column: str,
    columns: Sequence[str],
) -> tuple[str, Decimal | None, str | None, Any, int]:
    candidates: list[tuple[dict[str, Any], str, Decimal]] = []
    for row in rows:
        for column in columns:
            price = _positive(row.get(column))
            if price is not None:
                candidates.append((row, column, price))
                break
    if not candidates:
        return "missing", None, None, None, 0
    latest_time = max(_time_key(row.get(time_column)) for row, _, _ in candidates)
    latest = [item for item in candidates if _time_key(item[0].get(time_column)) == latest_time]
    variants = {(column, price) for _, column, price in latest}
    if len(variants) != 1:
        return "ambiguous", None, None, latest[0][0].get(time_column), len(variants)
    column, price = next(iter(variants))
    return "ok", price, column, latest[0][0].get(time_column), 1


def _mock_main_attributes(
    rows: Sequence[dict[str, Any]], resolved_key: Any
) -> list[dict[str, Any]]:
    valid = [
        row
        for row in rows
        if str(row.get("platform_goods_key")) == str(resolved_key)
        and row.get("is_active") in (1, True)
        and str(row.get("attribute_name") or "").strip()
        and str(row.get("attribute_value") or "").strip()
    ]
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        by_name[str(row["attribute_name"]).strip()].append(row)
    selected = []
    for name, candidates in by_name.items():
        best_precedence = max(
            (
                _time_key(item.get("last_upd_dt")),
                _time_key(item.get("effective_from")),
                _time_key(item.get("job_id")),
            )
            for item in candidates
        )
        row = min(
            (
                item
                for item in candidates
                if (
                    _time_key(item.get("last_upd_dt")),
                    _time_key(item.get("effective_from")),
                    _time_key(item.get("job_id")),
                )
                == best_precedence
            ),
            key=lambda item: str(item.get("attribute_value")),
        )
        selected.append(
            {
                "attribute_name": name,
                "attribute_value": str(row["attribute_value"]).strip(),
                "attribute_source": "d_platform_goods_attributes",
                "attribute_source_time": row.get("last_upd_dt"),
            }
        )
    return selected


def _mock_fallback_attributes(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = _latest_rows(rows, "month")  # Rank before filtering empty properties.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in latest:
        name = str(row.get("props_name") or "").strip()
        value = str(row.get("props_value") or "").strip()
        if name and value:
            unique[(name, value)] = {
                "attribute_name": name,
                "attribute_value": value,
                "attribute_source": "new_infinitus_attribute",
                "attribute_source_time": row.get("month"),
            }
    return [unique[key] for key in sorted(unique)]


def _merge_attributes(
    ssv4_attributes: Sequence[dict[str, Any]],
    infinitus_attributes: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected = list(ssv4_attributes)
    ssv4_names = {
        str(attribute.get("attribute_name") or "").strip()
        for attribute in ssv4_attributes
    }
    selected.extend(
        attribute
        for attribute in infinitus_attributes
        if str(attribute.get("attribute_name") or "").strip() not in ssv4_names
    )
    return selected


def resolve_mock_rows(
    identities: list[dict[str, Any]],
    *,
    goods_rows: Sequence[dict[str, Any]] = (),
    monthly_rows: Sequence[dict[str, Any]] = (),
    attribute_rows: Sequence[dict[str, Any]] = (),
    backup_rows: Sequence[dict[str, Any]] = (),
    relation_signature: Any = "mock",
    snapshot_at: Any = "mock",
    query_warnings: Sequence[dict[str, str]] = (),
) -> dict[str, dict[str, Any]]:
    """Offline implementation of the database precedence rules for tests."""
    normalized = _normalize_identities(identities)
    platform_counts = Counter(item["product_id"] for item in normalized)
    goods_has_platform = bool(_mock_platform_column(goods_rows))
    monthly_has_platform = bool(_mock_platform_column(monthly_rows))
    if goods_has_platform != monthly_has_platform:
        raise ValueError("mock goods/monthly platform columns must be both present or absent")

    output: dict[str, dict[str, Any]] = {}
    for item in normalized:
        platform = item["platform"]
        product_id = item["product_id"]
        goods, core_platform = _mock_scope(
            goods_rows, "platform_goods_id", product_id, platform
        )
        monthly, _ = _mock_scope(
            monthly_rows, "platform_goods_id", product_id, platform
        )
        backup, backup_platform = _mock_scope(
            backup_rows, "item_id", product_id, platform
        )
        blocked = not core_platform and platform_counts[product_id] > 1
        platform_status = "ambiguous" if blocked else ("exact" if core_platform else "unscoped_unique_input")

        goods_key, goods_variants, goods_key_time = _mock_key_choice(
            goods, "last_upd_dt"
        )
        monthly_key, monthly_variants, monthly_key_time = _mock_key_choice(
            monthly, "month"
        )
        source_conflict = (
            goods_variants == monthly_variants == 1
            and str(goods_key) != str(monthly_key)
        )
        unscoped_conflict = source_conflict and not core_platform
        if blocked:
            resolved_key, key_status, key_source, key_time = None, "identity_blocked", None, None
        elif goods_variants > 1 or unscoped_conflict:
            resolved_key, key_status, key_source, key_time = None, "ambiguous", None, None
        elif goods_key is not None:
            resolved_key, key_status = goods_key, "ok"
            key_source, key_time = "d_platform_goods", goods_key_time
        elif monthly_variants > 1:
            resolved_key, key_status, key_source, key_time = None, "ambiguous", None, None
        elif monthly_key is not None:
            resolved_key, key_status = monthly_key, "ok"
            key_source, key_time = (
                "mv_com_goods_statistics_monthly_v2_internal_ssv4",
                monthly_key_time,
            )
        else:
            resolved_key, key_status, key_source, key_time = None, "missing", None, None

        if key_source == "d_platform_goods":
            source_platform_key = _source_platform_key(
                goods, resolved_key, "last_upd_dt"
            )
        elif key_source == "mv_com_goods_statistics_monthly_v2_internal_ssv4":
            source_platform_key = _source_platform_key(monthly, resolved_key, "month")
        else:
            source_platform_key = None
        source_platform = _platform_source_info(platform, source_platform_key)

        main_attributes = (
            _mock_main_attributes(attribute_rows, resolved_key)
            if key_status == "ok"
            else []
        )
        backup_safe = backup_platform or platform_counts[product_id] == 1
        infinitus_attributes = (
            _mock_fallback_attributes(backup)
            if not blocked and key_status not in {"ambiguous", "identity_blocked"} and backup_safe
            else []
        )
        selected_attributes = _merge_attributes(main_attributes, infinitus_attributes)

        goods_name, goods_name_time = _mock_name(goods, "last_upd_dt")
        monthly_name, monthly_name_time = _mock_name(monthly, "month")
        backup_name_rows = [
            {
                "platform_goods_name": row.get("item_name"),
                "month": row.get("month"),
            }
            for row in backup
        ]
        backup_name, backup_name_time = _mock_name(backup_name_rows, "month")
        if blocked:
            product_name = name_source = name_time = None
        elif goods_name is not None:
            product_name, name_source, name_time = goods_name, "d_platform_goods", goods_name_time
        elif monthly_name is not None:
            product_name, name_source, name_time = (
                monthly_name,
                "mv_com_goods_statistics_monthly_v2_internal_ssv4",
                monthly_name_time,
            )
        elif backup_safe and backup_name is not None:
            product_name, name_source, name_time = (
                backup_name,
                "new_infinitus_attribute",
                backup_name_time,
            )
        else:
            product_name = name_source = name_time = None

        monthly_price_rows = monthly
        if key_status == "ok":
            monthly_price_rows = [
                row
                for row in monthly
                if str(row.get("platform_goods_key")) == str(resolved_key)
            ]
        monthly_price = _mock_price(
            monthly_price_rows,
            "month",
            ("lowest_promo_price", "avg_promo_price", "avg_price_m", "rrp"),
        )
        goods_price = _mock_price(
            goods, "last_upd_dt", ("current_price", "original_price")
        )
        if blocked:
            price_status, price, price_column, price_time, price_source = (
                "identity_blocked", None, None, None, None
            )
        elif monthly_price[0] != "missing":
            price_status, price, price_column, price_time = monthly_price[:4]
            price_source = (
                "mv_com_goods_statistics_monthly_v2_internal_ssv4"
                if price_status == "ok"
                else None
            )
        else:
            price_status, price, price_column, price_time = goods_price[:4]
            price_source = "d_platform_goods" if price_status == "ok" else None

        if price_source == "mv_com_goods_statistics_monthly_v2_internal_ssv4":
            price_platform_key = _source_platform_key(
                monthly_price_rows, resolved_key, "month"
            )
        elif price_source == "d_platform_goods":
            price_platform_key = _source_platform_key(
                goods, resolved_key, "last_upd_dt"
            )
        else:
            price_platform_key = None
        price_platform = _platform_source_info(platform, price_platform_key)

        flat = {
            "platform_status": platform_status,
            "candidate_row_count": len(goods) + len(monthly),
            "scoped_row_count": len(goods) + len(monthly),
            "resolved_key": resolved_key,
            "key_status": key_status,
            "key_source": key_source,
            "key_source_time": key_time,
            "source_platform_key": source_platform["key"],
            "source_platform_label": source_platform["label"],
            "goods_key": goods_key,
            "monthly_key": monthly_key,
            "goods_key_variant_count": goods_variants,
            "monthly_key_variant_count": monthly_variants,
            "key_source_conflict": int(source_conflict),
            "product_name": product_name,
            "name_source": name_source,
            "name_source_time": name_time,
            "price_status": price_status,
            "price": price,
            "price_source_table": price_source,
            "price_source_column": price_column,
            "price_source_time": price_time,
            "price_platform_key": price_platform["key"],
            "price_platform_label": price_platform["label"],
            "monthly_price_variant_count": monthly_price[4],
            "goods_price_variant_count": goods_price[4],
            "snapshot_at": snapshot_at,
        }
        result_rows = []
        if selected_attributes:
            for attribute in selected_attributes:
                result_rows.append({**flat, **attribute})
        else:
            result_rows.append(
                {
                    **flat,
                    "attribute_name": None,
                    "attribute_value": None,
                    "attribute_source": None,
                    "attribute_source_time": None,
                }
            )
        document = _assemble_result(
            item, result_rows, relation_signature
        )
        document["review_issues"].extend(query_warnings)
        output[identity_key(platform, product_id)] = document
    return output


def _self_test() -> None:
    identities = [
        {"platform": "jd", "product_id": "497394"},
        {"platform": "tmall", "product_id": "497394"},
        {"platform": "jd", "product_id": "fallback"},
        {"platform": "jd", "product_id": "ambiguous-price"},
    ]
    goods = [
        {
            "platform": "jd", "platform_goods_id": "497394",
            "platform_goods_key": 22129910, "platform_goods_name": "JD商品",
            "last_upd_dt": "2026-08-01", "current_price": "399", "original_price": "499",
        },
        {
            "platform": "tmall", "platform_goods_id": "497394",
            "platform_goods_key": 999, "platform_goods_name": "天猫商品",
            "last_upd_dt": "2026-08-02", "current_price": "299", "original_price": "399",
        },
        {
            "platform": "jd", "platform_goods_id": "fallback",
            "platform_goods_key": 300, "platform_goods_name": "备用属性商品",
            "last_upd_dt": "2026-08-01", "current_price": "80", "original_price": "100",
        },
        {
            "platform": "jd", "platform_goods_id": "ambiguous-price",
            "platform_goods_key": 400, "platform_goods_name": "歧义价格商品",
            "last_upd_dt": "2026-08-01", "current_price": "37", "original_price": "50",
        },
    ]
    monthly = [
        {
            "platform": "jd", "platform_goods_id": "497394",
            "platform_goods_key": 22129910, "platform_goods_name": "JD月度名",
            "month": "2026-07", "lowest_promo_price": "352.75",
            "avg_promo_price": "300", "avg_price_m": "360", "rrp": "500",
        },
        {
            "platform": "tmall", "platform_goods_id": "497394",
            "platform_goods_key": 999, "platform_goods_name": "天猫月度名",
            "month": "2026-07", "lowest_promo_price": "288",
            "avg_promo_price": None, "avg_price_m": None, "rrp": None,
        },
        {
            "platform": "jd", "platform_goods_id": "ambiguous-price",
            "platform_goods_key": 400, "platform_goods_name": "歧义价格商品",
            "month": "2026-07", "lowest_promo_price": "41",
            "avg_promo_price": None, "avg_price_m": None, "rrp": None,
        },
        {
            "platform": "jd", "platform_goods_id": "ambiguous-price",
            "platform_goods_key": 400, "platform_goods_name": "歧义价格商品",
            "month": "2026-07", "lowest_promo_price": "42",
            "avg_promo_price": None, "avg_price_m": None, "rrp": None,
        },
    ]
    attributes = [
        {
            "platform_goods_key": 22129910, "attribute_name": "品牌",
            "attribute_value": "主表品牌", "is_active": 1,
            "last_upd_dt": "2026-08-01", "effective_from": "2026-08-01", "job_id": 1,
        }
    ]
    backup = [
        {
            "platform": "jd", "item_id": "497394", "item_name": "备用JD名",
            "props_name": "不得补入", "props_value": "备用值", "month": "2026-08",
        },
        {
            "platform": "tmall", "item_id": "497394", "item_name": "备用天猫名",
            "props_name": "平台属性", "props_value": "天猫", "month": "2026-08",
        },
        {
            "platform": "jd", "item_id": "fallback", "item_name": "备用名",
            "props_name": "旧属性", "props_value": "旧值", "month": "2026-07",
        },
        {
            "platform": "jd", "item_id": "fallback", "item_name": "备用名",
            "props_name": "新属性", "props_value": "新值", "month": "2026-08",
        },
    ]
    result = resolve_mock_rows(
        identities,
        goods_rows=goods,
        monthly_rows=monthly,
        attribute_rows=attributes,
        backup_rows=backup,
    )
    jd = result[identity_key("jd", "497394")]
    tmall = result[identity_key("tmall", "497394")]
    fallback = result[identity_key("jd", "fallback")]
    ambiguous = result[identity_key("jd", "ambiguous-price")]

    assert jd["key"]["value"] == 22129910
    assert jd["price"]["value"] == Decimal("352.75")
    assert jd["price"]["source_column"] == "lowest_promo_price"
    assert {item["name"] for item in jd["attributes"]} == {"品牌", "不得补入"}
    assert tmall["key"]["value"] == 999 and tmall["name"]["value"] == "天猫商品"
    assert {item["name"] for item in fallback["attributes"]} == {"新属性"}
    assert fallback["price"]["value"] == Decimal("80")
    assert ambiguous["price"]["status"] == "ambiguous"
    assert ambiguous["price"]["value"] is None
    assert ambiguous["price"]["source_table"] is None

    unscoped = resolve_mock_rows(
        [
            {"platform": "jd", "product_id": "same-id"},
            {"platform": "tmall", "product_id": "same-id"},
        ],
        goods_rows=[
            {
                "platform_goods_id": "same-id", "platform_goods_key": 1,
                "platform_goods_name": "无法区分平台", "last_upd_dt": "2026-08-01",
                "current_price": "10", "original_price": "20",
            }
        ],
    )
    for platform in ("jd", "tmall"):
        unresolved = unscoped[identity_key(platform, "same-id")]
        assert unresolved["identity"]["platform_match_mode"] == "ambiguous"
        assert unresolved["key"]["value"] is None
    print("redshift_backend self-test: OK")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Redshift batch enrichment backend")
    parser.add_argument("--self-test", action="store_true", help="run offline checks")
    args = parser.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
