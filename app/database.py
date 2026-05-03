import os
from psycopg2 import pool
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set in environment")

# minconn=1 keeps one connection open always (fast first request)
# maxconn=5 stays well under Supabase free tier's 60-connection limit
connection_pool = pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=5,
    dsn=DATABASE_URL
)

def get_conn():
    """Borrow a connection from the pool."""
    return connection_pool.getconn()

def release_conn(conn):
    """Return a connection to the pool."""
    connection_pool.putconn(conn)

def close_all():
    """Close all connections — call on app shutdown."""
    connection_pool.closeall()