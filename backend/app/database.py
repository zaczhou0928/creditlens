"""唯一金融 Schema 入口，不扫描其他业务目录。"""

from .credit.schema import DATABASE, RELATIONS, SCHEMA, SCHEMA_VERSION


def physical_table_name(table: dict) -> str:
    return str(table["name"])


__all__ = ["DATABASE", "RELATIONS", "SCHEMA", "SCHEMA_VERSION", "physical_table_name"]
