"""Pure SQL allowlist checks shared with tests; DB read-only grants remain the hard boundary."""
import os, re
ALLOWED=set(os.getenv("ALLOWED_TABLES","customers,products,orders,order_items").split(","))
def validate_query(query: str) -> str:
    q=query.strip().rstrip(";").strip()
    if not q or len(q)>12000: raise ValueError("Query is empty or exceeds 12,000 characters")
    if ";" in q: raise ValueError("Only one SQL statement is allowed")
    if not re.match(r"^(SELECT|WITH)\b",q,re.I): raise ValueError("Only SELECT queries are allowed")
    if re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|CALL|DO|VACUUM|SET)\b",q,re.I): raise ValueError("Modifying or administrative SQL is prohibited")
    names=set(re.findall(r"\b(?:FROM|JOIN)\s+(?:([a-zA-Z_]\w*)\.)?[\"']?([a-zA-Z_][\w]*)",q,re.I))
    ctes=set(re.findall(r"(?:WITH|,)\s*([a-zA-Z_]\w*)\s+AS\s*\(",q,re.I))
    for schema,name in names:
        if schema and schema.lower()!="public": raise ValueError("Only the public schema is accessible")
        if name.lower() not in ALLOWED and name.lower() not in ctes: raise ValueError("Query references a table outside the allowed list")
    if re.search(r"\b(?:pg_catalog|information_schema)\s*\.",q,re.I): raise ValueError("System catalogs are not accessible")
    return q
