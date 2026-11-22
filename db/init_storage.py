"""Initialize PostgreSQL storage for accounts, chats, execution traces, and CSV uploads."""
import os

import psycopg
from psycopg import sql


# --- Load the database administrator connection ---
app_password = os.getenv("APP_PASSWORD", "app_dev")
database_url = os.getenv("DATABASE_ADMIN_URL")
connection = psycopg.connect(database_url) if database_url else psycopg.connect()


# --- Create application tables and indexes ---
with connection as conn:
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
          id UUID PRIMARY KEY, email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'detective_app') THEN
            CREATE ROLE detective_app LOGIN;
          END IF;
        END $$""")
        cur.execute(sql.SQL("ALTER ROLE detective_app PASSWORD {}").format(sql.Literal(app_password)))
        cur.execute("""CREATE TABLE IF NOT EXISTS conversations (
          id UUID PRIMARY KEY, title TEXT NOT NULL, messages JSONB NOT NULL DEFAULT '[]'::jsonb,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS owner_id UUID REFERENCES users(id) ON DELETE CASCADE")
        cur.execute("CREATE INDEX IF NOT EXISTS conversations_owner_updated_idx ON conversations(owner_id, updated_at DESC)")
        cur.execute("""CREATE TABLE IF NOT EXISTS uploaded_datasets (
          conversation_id UUID PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
          filename TEXT NOT NULL, csv_content BYTEA NOT NULL, columns JSONB NOT NULL,
          row_count INTEGER NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL)""")
        cur.execute("CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id)")
        cur.execute("""CREATE TABLE IF NOT EXISTS executions (
          id UUID PRIMARY KEY,
          conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          events JSONB NOT NULL DEFAULT '[]'::jsonb,
          status TEXT NOT NULL,
          error TEXT,
          duration_ms INTEGER,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("CREATE INDEX IF NOT EXISTS executions_owner_created_idx ON executions(owner_id, created_at DESC)")
        # --- Grant the application role its least-privilege access ---
        cur.execute("GRANT USAGE ON SCHEMA public TO detective_app")
        cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conversations, uploaded_datasets, users, sessions, executions TO detective_app")
        cur.execute("SELECT current_database()")
        database_name = cur.fetchone()[0]
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO detective_app").format(sql.Identifier(database_name)))

connection.close()
print("Initialized PostgreSQL account, session, chat, execution, and upload storage.")
