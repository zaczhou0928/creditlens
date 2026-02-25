from __future__ import annotations
from dataclasses import dataclass
from ..database import SCHEMA
import hashlib
import json

ALL_TABLES = frozenset(t["id"] for t in SCHEMA)
ALL_ORGS = frozenset({"O1", "O2", "O3", "O4"})
SENSITIVE = frozenset(
    f"{t['id']}.{f['name']}" for t in SCHEMA for f in t["fields"] if f.get("sensitive")
)


@dataclass(frozen=True)
class AccessScope:
    user_id: str
    roles: tuple[str, ...]
    allowed_databases: frozenset[str]
    allowed_tables: frozenset[str]
    allowed_orgs: frozenset[str] = frozenset()
    denied_fields: frozenset[str] = SENSITIVE

    def allows_database(self, database):
        return database in self.allowed_databases

    def allows_table(self, database, table_id):
        return self.allows_database(database) and table_id in self.allowed_tables

    def allows_field(self, table_id, name):
        return (
            self.allows_table("credit_ops", table_id)
            and f"{table_id}.{name}" not in self.denied_fields
        )

    def public(self):
        return {
            "user_id": self.user_id,
            "roles": list(self.roles),
            "allowed_databases": sorted(self.allowed_databases),
            "allowed_tables": sorted(self.allowed_tables),
            "allowed_orgs": sorted(self.allowed_orgs),
            "denied_fields": sorted(self.denied_fields),
        }

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(self.public(), sort_keys=True).encode()
        ).hexdigest()[:20]

    @classmethod
    def from_dict(cls, payload):
        raw = payload or {}
        return cls(
            str(raw.get("user_id", "anonymous")),
            tuple(raw.get("roles", [])),
            frozenset(raw.get("allowed_databases", [])),
            frozenset(raw.get("allowed_tables", [])),
            frozenset(raw.get("allowed_orgs", [])),
            frozenset(raw.get("denied_fields", SENSITIVE)),
        )


class AccessController:
    DEFAULT_USER = "credit_analyst"
    USER_ROLES = {
        "credit_admin": ("admin",),
        "credit_analyst": ("analyst",),
        "credit_east": ("regional",),
        "credit_risk": ("risk",),
    }

    def resolve(self, user_id=None):
        uid = user_id or self.DEFAULT_USER
        roles = self.USER_ROLES.get(uid, ())
        return AccessScope(
            uid,
            roles,
            frozenset({"credit_ops"}) if roles else frozenset(),
            ALL_TABLES if roles else frozenset(),
            frozenset({"O1", "O2"})
            if uid == "credit_east"
            else ALL_ORGS
            if roles
            else frozenset(),
            frozenset() if "admin" in roles else SENSITIVE,
        )

    @staticmethod
    def filter_schema(scope):
        return [
            {
                **t,
                "fields": [
                    f for f in t["fields"] if scope.allows_field(t["id"], f["name"])
                ],
            }
            for t in SCHEMA
            if scope.allows_table(t["database"], t["id"])
        ]
