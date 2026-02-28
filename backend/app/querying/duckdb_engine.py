from __future__ import annotations
import json
import re
from threading import Timer
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import duckdb
import sqlglot
from sqlglot import exp
from ..config import BASE_DIR
from ..database import SCHEMA
from ..security import AccessController
from .models import SqlExecution

DATABASE_ROOT = BASE_DIR / "data/databases"


class DuckDbEngine:
    """显式类型导入；只暴露受机构和字段权限约束的金融视图。"""

    def __init__(self, database_root=None):
        self.database_root = database_root or DATABASE_ROOT

    def manifest(self):
        return json.loads(
            (self.database_root / "credit_ops/_database_manifest.json").read_text(
                encoding="utf-8"
            )
        )

    def execute(self, database, sql, access_scope=None):
        scope = access_scope or AccessController().resolve()
        try:
            cleaned = self._validate_sql(database, sql, scope)
            use_snapshots = any(
                t.name == "loan_daily_snapshots"
                for t in sqlglot.parse_one(cleaned, read="duckdb").find_all(exp.Table)
            )
            with self.connect(database, scope, include_snapshots=use_snapshots) as con:
                timer = Timer(10, con.interrupt)
                timer.daemon = True
                timer.start()
                try:
                    cursor = con.execute(cleaned)
                    data = cursor.fetchmany(201)
                finally:
                    timer.cancel()
                    timer.join()
                columns = [x[0] for x in cursor.description or []]
                if len(set(columns)) != len(columns):
                    raise ValueError("输出列名重复，请为每列设置不同别名")
                metadata = {}
                for name, kind, *_ in cursor.description or []:
                    source_type = str(kind)
                    numeric = any(
                        t in source_type for t in ("INT", "DECIMAL", "DOUBLE", "FLOAT")
                    )
                    # Only the DB type is authoritative; units/metric semantics must not be guessed from aliases.
                    metadata[name] = {
                        "kind": "number" if numeric else "dimension",
                        "source_type": source_type,
                        "unit": "",
                        "precision": None,
                    }
                rows = [
                    dict(zip(columns, [self._json_value(v) for v in row]))
                    for row in data[:200]
                ]
            manifest = self.manifest()
            return SqlExecution(
                cleaned,
                True,
                columns,
                rows,
                truncated=len(data) > 200,
                returned_rows=len(rows),
                total_rows=len(rows) if len(data) <= 200 else None,
                data_version=manifest["data_version"],
                data_as_of=manifest["data_as_of"],
                column_metadata=metadata,
            )
        except (ValueError, sqlglot.errors.ParseError, duckdb.Error, OSError) as exc:
            return SqlExecution(sql, False, error=str(exc))

    @contextmanager
    def connect(
        self, database="credit_ops", access_scope=None, *, include_snapshots=True
    ):
        scope = access_scope or AccessController().resolve()
        if database != "credit_ops" or not scope.allows_database(database):
            raise ValueError("无权访问金融分析库")
        folder = self.database_root / database
        con = duckdb.connect(":memory:", config={"threads": 2, "memory_limit": "512MB"})
        try:
            loaded = set()
            for table in SCHEMA:
                name = table["name"]
                if name == "loan_daily_snapshots" and (
                    not include_snapshots or not (folder / f"{name}.csv").exists()
                ):
                    continue
                path = str((folder / f"{name}.csv").resolve()).replace("'", "''")
                fields = ", ".join(
                    "'" + f["name"] + "': '" + f["source_type"] + "'"
                    for f in table["fields"]
                )
                con.execute(
                    f"CREATE TABLE __raw_{name} AS SELECT * FROM read_csv('{path}', header=true, columns={{{fields}}})"
                )
                loaded.add(name)
            orgs = (
                ",".join(
                    "'" + o.replace("'", "''") + "'" for o in sorted(scope.allowed_orgs)
                )
                or "NULL"
            )
            org_filter = f"org_id IN ({orgs})"
            loan_filter = (
                f"loan_id IN (SELECT loan_id FROM __raw_loans WHERE {org_filter})"
            )
            for table in SCHEMA:
                if not scope.allows_table(database, table["id"]):
                    continue
                name = table["name"]
                if name not in loaded:
                    continue
                columns = ", ".join(
                    '"' + f["name"] + '"'
                    for f in table["fields"]
                    if scope.allows_field(table["id"], f["name"])
                )
                if not columns:
                    continue
                if name in {"customers", "applications", "loans", "org_units"}:
                    predicate = org_filter
                elif name == "approval_events":
                    predicate = f"application_id IN (SELECT application_id FROM __raw_applications WHERE {org_filter})"
                elif name == "repayment_allocations":
                    predicate = f"transaction_id IN (SELECT transaction_id FROM __raw_repayment_transactions WHERE {loan_filter})"
                elif name in {
                    "disbursements",
                    "repayment_schedules",
                    "repayment_transactions",
                    "loan_daily_snapshots",
                }:
                    predicate = loan_filter
                else:
                    predicate = "TRUE" if scope.allowed_orgs else "FALSE"
                con.execute(
                    f'CREATE TABLE "{name}" AS SELECT {columns} FROM __raw_{name} WHERE {predicate}'
                )
            # No unrestricted backing tables remain in the execution connection. This also blocks
            # CTE-name shadowing and dynamic resolution from reaching another institution's rows.
            for name in loaded:
                con.execute(f"DROP TABLE __raw_{name}")
            con.execute("SET enable_external_access=false")
            yield con
        finally:
            con.close()

    def _validate_sql(self, database, sql, access_scope=None):
        scope = access_scope or AccessController().resolve()
        if database != "credit_ops" or not scope.allows_database(database):
            raise ValueError("无权访问数据库")
        cleaned = self.clean_sql(sql).rstrip(";").strip()
        trees = [t for t in sqlglot.parse(cleaned, read="duckdb") if t]
        if len(trees) != 1 or not isinstance(trees[0], exp.Query):
            raise ValueError("只允许一条SELECT/WITH只读查询")
        tree = trees[0]
        forbidden = {
            "insert",
            "update",
            "delete",
            "merge",
            "create",
            "drop",
            "alter",
            "copy",
            "attach",
            "detach",
            "command",
            "transaction",
            "grant",
            "revoke",
            "into",
        }
        if any(node.key in forbidden for node in tree.walk()):
            raise ValueError("禁止写入或管理操作")
        for select in tree.find_all(exp.Select):
            if any(
                isinstance(p, exp.Star) or isinstance(p, exp.Column) and p.is_star
                for p in select.expressions
            ):
                raise ValueError("必须明确返回字段，不允许SELECT *")
        ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        allowed = {t["name"]: t["id"] for t in SCHEMA}
        for table in tree.find_all(exp.Table):
            if table.db or table.catalog or not isinstance(table.this, exp.Identifier):
                raise ValueError("禁止限定库名、外部数据和动态表函数")
            if table.name in ctes:
                if table.name.startswith("__"):
                    raise ValueError("禁止引用内部表名")
                continue
            if table.name not in allowed or not scope.allows_table(
                database, allowed[table.name]
            ):
                raise ValueError("SQL引用未知或无权访问的表")
        # Anonymous functions could evaluate SQL, access configuration or discover internal tables.
        if any(True for _ in tree.find_all(exp.Anonymous)):
            raise ValueError("不支持未登记的SQL函数")
        return cleaned

    @staticmethod
    def clean_sql(text):
        return re.sub(r"^```(?:sql)?\s*|\s*```$", "", text.strip(), flags=re.I).strip()

    @staticmethod
    def _json_value(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        return value
