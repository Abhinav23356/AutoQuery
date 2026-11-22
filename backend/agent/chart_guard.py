"""Pure chart payload validation used by the chart MCP server."""
from typing import Any


# --- Validate chart data and build the UI payload ---
def chart_config(kind:str,data:list[dict[str,Any]],title:str,x:str,y:str,series:list[str]|None=None)->dict:
    if not isinstance(data,list) or not data: raise ValueError("Chart data must contain at least one row")
    if len(data)>500: raise ValueError("Chart data is limited to 500 rows")
    fields=list(dict.fromkeys(series or [y]))
    if len(fields)>8: raise ValueError("Charts support at most eight value series")
    if any(not isinstance(row,dict) or x not in row or any(field not in row for field in fields) for row in data): raise ValueError("Each row must include the requested x column and every value series")
    if any(not isinstance(row[field],(int,float)) or isinstance(row[field],bool) for row in data for field in fields): raise ValueError("All chart value series must be numeric")
    return {"type":kind,"title":title[:120],"data":data,"xAxis":x,"yAxis":y,"series":fields,"reference":f"chart-{kind}","created":True}
