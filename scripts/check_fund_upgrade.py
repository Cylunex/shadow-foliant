"""Describe fund-v2 readiness without connecting unless --check is explicitly supplied."""

from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="只读检查已配置数据库，不执行迁移"
    )
    args = parser.parse_args()
    if not args.check:
        print("迁移来源: scripts/migrations/22-fund-research-upgrade.sql")
        print("按运维约定备份并执行标准迁移后，使用 --check 做只读就绪检查。")
        return 0
    import fund_db

    conn = fund_db._conn()
    try:
        cur = conn.cursor()
        for table in ("fund_ledger_openings", "fund_ledger_events", "fund_workspace"):
            cur.execute("SELECT COUNT(*) FROM " + table)
            print(table + ": ready")
        cur.execute("SELECT COUNT(*) FROM fund_transactions WHERE source='dca_auto'")
        print("legacy_auto_record_review_required: " + str(cur.fetchone()[0]))
        cur.execute(
            "SELECT 1 FROM research_schema_migrations WHERE version=?",
            ("22-fund-research-upgrade",),
        )
        if not cur.fetchone():
            print("migration_version: not_recorded")
            return 1
        print("migration_version: recorded")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
