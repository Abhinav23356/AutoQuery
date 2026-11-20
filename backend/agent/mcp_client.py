"""Owns genuine stdio MCP client sessions and exposes discovered tool metadata."""
import json, os, sys
from contextlib import AsyncExitStack
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
class MCPConnections:
    def __init__(self): self.stack=AsyncExitStack(); self.sessions={}
    async def __aenter__(self):
        try:
            for name,file,env in [("chart-mcp-server","chart_server.py",os.environ.copy())]:
                env={**env,"PYTHONPATH":str(ROOT)+(os.pathsep+env["PYTHONPATH"] if env.get("PYTHONPATH") else "")}
                params=StdioServerParameters(command=sys.executable,args=[str(ROOT/"mcp_servers"/file)],env=env)
                read,write=await self.stack.enter_async_context(stdio_client(params))
                session=await self.stack.enter_async_context(ClientSession(read,write)); await session.initialize()
                self.sessions[name]=session
            return self
        except BaseException:
            await self.stack.aclose()
            raise
    async def __aexit__(self,*args): await self.stack.aclose()
    async def tools(self):
        found=[]
        for server,session in self.sessions.items():
            response=await session.list_tools()
            for tool in response.tools:
                found.append({"type":"function","function":{"name":tool.name,"description":f"[{server}] {tool.description or ''}","parameters":tool.inputSchema}})
        return found
    async def call(self,name,args):
        for server,session in self.sessions.items():
            tools=(await session.list_tools()).tools
            if any(t.name==name for t in tools):
                result=await session.call_tool(name,arguments=args)
                if result.isError: raise RuntimeError(str(result.content))
                texts=[c.text for c in result.content if getattr(c,"text",None)]
                try: return json.loads(texts[0]) if texts else {}
                except (ValueError,IndexError): return {"result":texts}
        raise ValueError(f"Discovered tool not found: {name}")
