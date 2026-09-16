import importlib.util
import unittest
from pathlib import Path

from psycopg import sql

from demo import postgres_connection_info
from redshift_backend import (
    ATTRIBUTE_QUERY_TIMEOUT_MS,
    MONTHLY_QUERY_TIMEOUT_MS,
    OBJECTS,
    REQUIRED_COLUMNS,
    _build_lookup_query,
    _build_query,
    _resolve_relations,
    resolve_mock_rows,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.description = [type("Column", (), {"name": name}) for name in (
            "table_schema", "table_name", "column_name", "data_type", "ordinal_position"
        )]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        pass

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


def _catalog_without_optional_postgresql_objects():
    rows = []
    for logical_name in ("goods", "monthly", "attributes"):
        columns = REQUIRED_COLUMNS[logical_name] - ({"rrp"} if logical_name == "monthly" else set())
        for position, name in enumerate(sorted(columns), start=1):
            rows.append(("workdb", OBJECTS[logical_name], name, "character varying", position))
    return rows


class PostgreSQLSchemaTests(unittest.TestCase):
    def test_attribute_lookup_has_a_five_minute_timeout_for_full_results(self):
        self.assertEqual(ATTRIBUTE_QUERY_TIMEOUT_MS, 300_000)

    def test_three_core_postgresql_tables_allow_missing_backup_and_rrp(self):
        schema, relations = _resolve_relations(
            _Connection(_catalog_without_optional_postgresql_objects()), "workdb"
        )

        self.assertEqual(schema, "workdb")
        self.assertTrue(relations["backup"]["synthetic"])
        self.assertNotIn("rrp", relations["monthly"]["columns"])
        query, _ = _build_query(
            sql, relations, [{"platform": "jd", "product_id": "497394"}]
        )
        rendered = query.as_string(None)
        self.assertIn("WHERE FALSE", rendered)
        self.assertIn("CAST(NULL AS NUMERIC) AS rrp", rendered)
        self.assertIn("CURRENT_TIMESTAMP AS snapshot_at", rendered)
        self.assertNotIn("GETDATE()", rendered)

    def test_frontend_exports_postgresql_connection_settings(self):
        app_path = Path(__file__).with_name("直接用图片测试.py")
        spec = importlib.util.spec_from_file_location("ocr_frontend", app_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        values = {
            "root": "D:/images",
            "platform": "jd",
            "paddle_ocr_api_url": "http://192.168.1.115:8870/v1/ocr",
            "paddle_ocr_model_version": "PaddleOCR-VL-1.6",
            "pg_host": "10.0.0.1",
            "pg_port": "5432",
            "pg_database": "products",
            "pg_user": "readonly",
            "pg_schema": "workdb",
            "pg_sslmode": "verify-full",
            "pg_password": "not-saved",
            "qwen_key": "not-saved",
            "ocr_workers": "2",
        }

        env = module.child_environment(values)

        self.assertEqual(env["POSTGRES_HOST"], "10.0.0.1")
        self.assertEqual(env["POSTGRES_SCHEMA"], "workdb")
        self.assertEqual(env["POSTGRES_SSLMODE"], "verify-full")
        self.assertNotIn("REDSHIFT_HOST", env)

    def test_postgresql_connection_has_a_bounded_statement_timeout(self):
        connection = postgres_connection_info(
            host="192.168.0.23",
            port=5432,
            database="products",
            user="readonly",
            password="not-saved",
            sslmode="disable",
        )

        self.assertEqual(connection["options"], "-c statement_timeout=60000")

    def test_postgresql_fast_lookup_is_a_single_indexable_predicate(self):
        _, relations = _resolve_relations(
            _Connection(_catalog_without_optional_postgresql_objects()), "workdb"
        )

        rendered = _build_lookup_query(
            sql,
            relations["monthly"],
            ("platform_goods_id", "platform_goods_key", "month"),
            "platform_goods_id",
        ).as_string(None)

        self.assertNotIn("WITH", rendered)
        self.assertIn('FROM "workdb"."mv_com_goods_statistics_monthly_v2_internal_ssv4"', rendered)
        self.assertIn('WHERE "platform_goods_id" = ANY(%s)', rendered)

    def test_required_ssv4_lookups_wait_up_to_five_minutes(self):
        self.assertEqual(MONTHLY_QUERY_TIMEOUT_MS, 300_000)
        self.assertEqual(ATTRIBUTE_QUERY_TIMEOUT_MS, 300_000)

    def test_ssv4_attributes_take_priority_and_infinitus_fills_missing_fields(self):
        result = resolve_mock_rows(
            [{"platform": "jd", "product_id": "p1"}],
            goods_rows=[{
                "platform_goods_id": "p1",
                "platform_goods_key": 1,
                "platform_goods_name": "商品",
                "last_upd_dt": "2026-09-14",
                "current_price": "99",
                "original_price": "119",
            }],
            attribute_rows=[{
                "platform_goods_key": 1,
                "attribute_name": "品牌",
                "attribute_value": "SSV4品牌",
                "is_active": 1,
                "last_upd_dt": "2026-09-14",
                "effective_from": "2026-09-14",
                "job_id": 1,
            }],
            backup_rows=[{
                "item_id": "p1",
                "item_name": "无限极商品",
                "props_name": "品牌",
                "props_value": "无限极品牌",
                "month": "2026-09",
            }, {
                "item_id": "p1",
                "item_name": "无限极商品",
                "props_name": "剂型",
                "props_value": "片剂",
                "month": "2026-09",
            }],
        )

        document = result["jd\x1fp1"]
        attributes = {item["name"]: item for item in document["attributes"]}
        self.assertEqual(attributes["品牌"]["value"], "SSV4品牌")
        self.assertEqual(attributes["品牌"]["source_table"], "d_platform_goods_attributes")
        self.assertEqual(attributes["剂型"]["value"], "片剂")
        self.assertEqual(attributes["剂型"]["source_table"], "new_infinitus_attribute")


if __name__ == "__main__":
    unittest.main()
