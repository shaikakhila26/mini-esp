import os

import psycopg


def get_connection():
    """Create a connection to PostgreSQL using environment settings."""
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "postgres"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "mini_esp"),
        user=os.environ.get("DB_USER", "esp_app"),
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )


def check_database() -> bool:
    """Return True if PostgreSQL accepts a simple query."""
    try:
        with get_connection() as connection:
            connection.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False