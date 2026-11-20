"""Per-conversation, in-memory CSV datasets with read-only SQL access."""
import csv
import io
import os
import re
import sqlite3
from dataclasses import dataclass

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))
MAX_ROWS = 20_000
TABLE = "uploaded_data"


@dataclass
class UploadedDataset:
    name: str
    columns: list[str]
    row_count: int
    connection: sqlite3.Connection

    @classmethod
    def from_csv(cls, filename: str, content: bytes):
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(f"CSV must be {MAX_UPLOAD_BYTES / (1024 * 1024):.1f} MB or smaller.")
        try:
            text = content.decode("utf-8-sig")
            reader = csv.reader(io.StringIO(text))
            headers = next(reader, None)
        except (UnicodeDecodeError, csv.Error):
            raise ValueError("Could not read this file as UTF-8 CSV.") from None
        if not headers or not any(h.strip() for h in headers):
            raise ValueError("The CSV needs a header row with column names.")
        if len(headers) > 100:
            raise ValueError("CSV can have at most 100 columns.")

        columns, used = [], set()
        for index, header in enumerate(headers, 1):
            base = re.sub(r"\W+", "_", header.strip(), flags=re.UNICODE).strip("_") or f"column_{index}"
            if base[0].isdigit():
                base = f"column_{base}"
            name, suffix = base, 2
            while name.lower() in used:
                name, suffix = f"{base}_{suffix}", suffix + 1
            used.add(name.lower())
            columns.append(name)

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        quoted = ", ".join('"' + c.replace('"', '""') + '" TEXT' for c in columns)
        conn.execute(f'CREATE TABLE "{TABLE}" ({quoted})')
        marks = ",".join("?" for _ in columns)
        row_count = 0
        try:
            for row in reader:
                if row_count >= MAX_ROWS:
                    raise ValueError(f"CSV can have at most {MAX_ROWS:,} data rows.")
                row = (row + [""] * len(columns))[:len(columns)]
                conn.execute(f'INSERT INTO "{TABLE}" VALUES ({marks})', row)
                row_count += 1
        except csv.Error:
            conn.close()
            raise ValueError("The CSV contains malformed rows.") from None
        conn.commit()
        return cls(filename or "uploaded.csv", columns, row_count, conn)

    def query(self, sql: str):
        statement = sql.strip()
        if not re.match(r"(?is)^\s*(select|with)\b", statement):
            raise ValueError("Only read-only SELECT queries are allowed.")
        if ";" in statement.rstrip(";\n\t "):
            raise ValueError("Only one SQL statement is allowed.")

        def authorize(action, arg1, arg2, _db, _source):
            if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_OK if arg1 == TABLE else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY

        self.connection.set_authorizer(authorize)
        try:
            cursor = self.connection.execute(statement)
            rows = cursor.fetchmany(501)
            return {"columns": [x[0] for x in cursor.description or []],
                    "rows": [dict(zip([x[0] for x in cursor.description or []], row)) for row in rows[:500]],
                    "row_count": len(rows), "truncated": len(rows) > 500}
        finally:
            self.connection.set_authorizer(None)

    def tool(self):
        schema = ", ".join(f'"{c}"' for c in self.columns)
        return {"type": "function", "function": {
            "name": "query_uploaded_dataset",
            "description": f"[Uploaded dataset] Run one read-only SQLite SELECT against this chat's uploaded_data table. Columns: {schema}. All columns are TEXT; cast numeric/date values as needed. Maximum 500 rows returned.",
            "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": f"SELECT query using only uploaded_data. Available columns: {schema}"}}, "required": ["sql"], "additionalProperties": False}}}
