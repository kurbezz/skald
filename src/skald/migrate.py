"""Offline audit and repair for organized-file rows orphaned from media jobs."""

import argparse
import csv
import json
from pathlib import Path

from sqlalchemy import bindparam, text

from skald.db import get_engine


ORPHAN_FIELDS = (
    "id",
    "job_id",
    "path",
    "operation_token",
    "lifecycle",
    "staging_path",
    "staging_device",
    "staging_inode",
    "published_device",
    "published_inode",
)


def _write_audit(rows: list[dict], audit_path: Path, audit_format: str) -> None:
    if audit_format == "json":
        with audit_path.open("w", encoding="utf-8") as audit_file:
            json.dump(rows, audit_file)
        return
    with audit_path.open("w", encoding="utf-8", newline="") as audit_file:
        writer = csv.DictWriter(audit_file, fieldnames=ORPHAN_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def export_and_purge_orphans(engine, audit_path: str | Path, *, audit_format: str) -> int:
    """Export and remove only ledger rows without a matching media job.

    The caller supplies the engine so this utility never changes SQLite's FK
    pragma. The audit write and deletion share one database transaction.
    """
    if audit_format not in {"json", "csv"}:
        raise ValueError("audit_format must be 'json' or 'csv'")
    audit_path = Path(audit_path)
    delete = text("DELETE FROM organizedfile WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    with engine.begin() as connection:
        columns = {
            column[1]
            for column in connection.exec_driver_sql("PRAGMA table_info(organizedfile)").fetchall()
        }
        select_columns = [
            f"organizedfile.{field} AS {field}"
            if field in columns else f"NULL AS {field}"
            for field in ORPHAN_FIELDS
        ]
        query = text(
            f"SELECT {', '.join(select_columns)} "
            "FROM organizedfile LEFT JOIN mediajob ON mediajob.id = organizedfile.job_id "
            "WHERE mediajob.id IS NULL ORDER BY organizedfile.id"
        )
        rows = [dict(row) for row in connection.execute(query).mappings().all()]
        _write_audit(rows, audit_path, audit_format)
        if rows:
            connection.execute(delete, {"ids": [row["id"] for row in rows]})
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("audit_path")
    parser.add_argument("--format", required=True, choices=("json", "csv"))
    args = parser.parse_args()
    engine = get_engine(args.database, enforce_foreign_keys=False)
    count = export_and_purge_orphans(engine, args.audit_path, audit_format=args.format)
    print(f"exported and purged {count} orphan organizedfile rows")


if __name__ == "__main__":
    main()
