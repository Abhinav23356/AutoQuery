"""Chart configuration MCP server. It validates data and returns Recharts-ready configs."""
from typing import Any
from mcp.server.fastmcp import FastMCP
from agent.chart_guard import chart_config
mcp=FastMCP("chart-mcp-server")

@mcp.tool()
def generate_bar_chart(data:list[dict[str,Any]],title:str,x_axis:str,y_axis:str,series:list[str]|None=None)->dict:
    """Return a Recharts bar configuration. Set series to every numeric column to compare multiple categories (for example regions) across the x axis."""
    return chart_config("bar",data,title,x_axis,y_axis,series)
@mcp.tool()
def generate_line_chart(data:list[dict[str,Any]],title:str,x_axis:str,y_axis:str,series:list[str]|None=None)->dict:
    """Return a Recharts line configuration. Set series to every numeric column to compare multiple categories (for example regions) across the x axis."""
    return chart_config("line",data,title,x_axis,y_axis,series)
@mcp.tool()
def generate_pie_chart(data:list[dict[str,Any]],title:str,label_column:str,value_column:str)->dict:
    """Validate data and return Recharts pie chart configuration."""
    return chart_config("pie",data,title,label_column,value_column)
if __name__=="__main__": mcp.run(transport="stdio")
