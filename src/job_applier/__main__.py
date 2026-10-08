"""Local CLI: initialize storage or inspect record counts."""
import argparse
import logging
import sqlite3
import sys
from job_applier.config import Settings
from job_applier.database.sqlite import TABLES
from job_applier.workflows.local import initialize_local_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Job Application Agent foundation (Phase 1.5)")
    parser.add_argument("command", choices=("init", "status", "worker-interface"))
    parser.add_argument("--worker-id", help="Trusted launcher-assigned worker identity")
    parser.add_argument("--candidate-id", help="Trusted launcher-assigned candidate scope")
    args = parser.parse_args()
    if args.command == "worker-interface" and (not args.worker_id or not args.candidate_id):
        parser.error("worker-interface requires --worker-id and --candidate-id")
    try:
        settings = Settings.load()
        logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        if args.command != "init" and not settings.database_path.is_file():
            print("Database does not exist. Run 'init' first.")
            return 1
        if args.command == "init":
            database = initialize_local_database(settings)
        else:
            from job_applier.database.sqlite import SQLiteDatabase
            database = SQLiteDatabase(settings.database_path)
        if args.command == "worker-interface":
            from job_applier.interfaces.worker import WorkerScope, serve_worker_stream
            serve_worker_stream(database, WorkerScope(args.worker_id, args.candidate_id), sys.stdin.buffer, sys.stdout.buffer)
        elif args.command == "init":
            print(f"Local database initialized: {settings.database_path}")
            print("External actions are disabled. No candidate data has been invented.")
        else:
            with database.transaction() as repo:
                for model, (table, _) in TABLES.items():
                    print(f"{table}: {repo.count(model)}")
        return 0
    except (ValueError, LookupError, OSError, sqlite3.Error, RuntimeError) as exc:
        logging.getLogger(__name__).error("Local operation failed (%s). Check settings and database access.", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
