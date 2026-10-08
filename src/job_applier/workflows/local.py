"""Explicit initialization; importing the package does not create a database."""
from job_applier.config import Settings
from job_applier.database.sqlite import SQLiteDatabase


def initialize_local_database(settings: Settings) -> SQLiteDatabase:
    database = SQLiteDatabase(settings.database_path)
    database.initialize()
    return database
