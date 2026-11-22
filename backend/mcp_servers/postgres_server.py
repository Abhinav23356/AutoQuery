"""Read-only PostgreSQL MCP server. Run as an independent stdio process."""
import json, os
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import create_engine, inspect, text
from mcp.server.fastmcp import FastMCP
from agent.sql_guard import validate_query


# --- Database connection and read-only limits ---
mcp = FastMCP("postgres-mcp-server")
engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True, connect_args={"options": "-c statement_timeout=5000 -c default_transaction_read_only=on"})
ALLOWED = set(os.getenv("ALLOWED_TABLES", "customers,products,orders,order_items").split(","))


# --- Convert database values for JSON responses ---
def _safe(value):
    if isinstance(value, Decimal): return float(value)
    if isinstance(value, (date, datetime)): return value.isoformat()
    return value


# --- Enforce the allowed-table list ---
def _table(name):
    if name not in ALLOWED: raise ValueError("Table is not in the allowed table list")
    if name not in inspect(engine).get_table_names(): raise ValueError("Unknown table")



# --- Schema inspection tool ---
@mcp.tool()
def inspect_schema() -> dict:
    """Return allowed table columns, types, primary keys, and foreign keys."""
    i=inspect(engine); result=[]
    for table in sorted(ALLOWED & set(i.get_table_names())):
        result.append({"table":table,"columns":[{"name":c["name"],"type":str(c["type"]),"nullable":c["nullable"]} for c in i.get_columns(table)],"primary_keys":i.get_pk_constraint(table).get("constrained_columns",[]),"foreign_keys":i.get_foreign_keys(table)})
    return {"tables":result}


# --- Read-only SQL query tool ---
@mcp.tool()
def execute_sql(query: str) -> dict:
    """Run a single read-only SELECT query. Results are capped at 500 rows and 5 seconds."""
    q=validate_query(query)
    with engine.connect() as conn:
        rows=conn.execute(text(f"SELECT * FROM ({q}) AS detective_result LIMIT 501")).mappings().fetchmany(501)
    truncated=len(rows)>500
    return {"columns":list(rows[0].keys()) if rows else [],"rows":[{k:_safe(v) for k,v in r.items()} for r in rows[:500]],"row_count":min(len(rows),500),"truncated":truncated}


# --- Table sample tool ---
@mcp.tool()
def get_table_sample(table_name: str) -> dict:
    """Return up to five sample rows from an allowed table."""
    _table(table_name)
    with engine.connect() as c: rows=c.execute(text(f'SELECT * FROM "{table_name}" LIMIT 5')).mappings().all()
    return {"table":table_name,"rows":[{k:_safe(v) for k,v in r.items()} for r in rows]}


# --- Table statistics tool ---
@mcp.tool()
def get_table_statistics(table_name: str) -> dict:
    """Return row count, per-column null counts, and numeric summaries."""
    _table(table_name); i=inspect(engine); cols=i.get_columns(table_name)
    with engine.connect() as c:
        count=c.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one()
        nulls={col["name"]:c.execute(text(f'SELECT COUNT(*) FROM "{table_name}" WHERE "{col["name"]}" IS NULL')).scalar_one() for col in cols}
        numeric={}
        for col in cols:
            if any(t in str(col["type"]).upper() for t in ("INT","NUMERIC","DECIMAL","FLOAT","REAL","DOUBLE")):
                numeric[col["name"]]={k:_safe(v) for k,v in c.execute(text(f'SELECT MIN("{col["name"]}") AS min, AVG("{col["name"]}") AS avg, MAX("{col["name"]}") AS max FROM "{table_name}"')).mappings().one().items()}
    return {"table":table_name,"row_count":count,"null_counts":nulls,"numeric_statistics":numeric}


# --- Start the stdio MCP server ---
if __name__ == "__main__": mcp.run(transport="stdio")
