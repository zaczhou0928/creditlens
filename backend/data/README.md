# Local data

Synthetic CSV tables, the schema manifest, retrieval indexes, session archives, and evaluation outputs are generated here and excluded from Git.

From `backend`, run `python seed_demo_data.py` to reproduce the dataset. The API also seeds missing data on first startup. The generator uses seed `20261006`, covers 2025-01-01 through 2026-09-30, and records table counts and file hashes. The default version is `0f974d296f352e6a`.

These fixture dates are independent of the retrospective commit schedule. No real customer or credit data is included.
