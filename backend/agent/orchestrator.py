"""AutoGen-based agent orchestration for Gemini and the project's MCP tools."""
import inspect
import json
import logging
import os
import time
from typing import Annotated, Any, Union

from autogen_agentchat.agents import AssistantAgent
from autogen_core.tools import FunctionTool
from autogen_ext.models.openai import OpenAIChatCompletionClient


# --- Agent identity and operating instructions ---
log = logging.getLogger("detective.agent")

SYSTEM = """You are Data Detective, a closed-world analytics agent. Answer factual questions only with evidence from this conversation's uploaded dataset or verified prior conversation context. Never use pretrained general knowledge, web knowledge, guesses, or another chat's data. Query only this chat's uploaded_data table with query_uploaded_dataset; its columns are TEXT, so cast numeric/date values as needed. If the requested fact is absent from this dataset, say it is not available. For vague questions, clarify the metric or timeframe when needed; otherwise state assumptions and investigate the data. SQL must be read-only and use only the listed table. Verify findings by checking relevant values, date coverage, nulls, calculations, and chart data. Distinguish observed patterns from proven causes. For charts comparing categories over time, use one row per time period and a separate numeric column for every category, then pass all series to the chart tool. Give concise Markdown with concrete values, dates, and caveats. Never reveal private reasoning."""



# --- Convert MCP JSON schemas into Python tool signatures ---
def _python_type(schema: dict[str, Any]) -> Any:
    """Convert the common JSON Schema shapes used by our MCP servers to Python types."""
    if "anyOf" in schema:
        options = [_python_type(item) for item in schema["anyOf"] if item.get("type") != "null"]
        if schema.get("nullable") or any(item.get("type") == "null" for item in schema["anyOf"]):
            options.append(type(None))
        return Union[tuple(options)] if len(options) > 1 else options[0]
    kind = schema.get("type")
    if isinstance(kind, list):
        options = [_python_type({**schema, "type": item}) for item in kind]
        return Union[tuple(options)]
    if kind == "string":
        return str
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "boolean":
        return bool
    if kind == "array":
        return list[_python_type(schema.get("items", {}))]
    if kind == "object":
        return dict[str, Any]
    return Any



# --- Adapt MCP tools for AutoGen ---
def _make_autogen_tool(spec: dict[str, Any], invoke):
    """Expose an MCP JSON-schema tool as an AutoGen FunctionTool."""
    function = spec["function"]
    name = function["name"]
    description = function.get("description", "")
    parameters = function.get("parameters", {})
    properties = parameters.get("properties", {})
    required = set(parameters.get("required", []))
    signature_parameters = []
    annotations: dict[str, Any] = {}

    async def call_tool(**kwargs: Any) -> str:
        return await invoke(name, kwargs)

    call_tool.__name__ = name
    call_tool.__doc__ = description
    for field_name, field_schema in properties.items():
        field_type = _python_type(field_schema)
        field_description = field_schema.get("description", field_name)
        annotation = Annotated[field_type, field_description]
        annotations[field_name] = annotation
        default = inspect.Parameter.empty if field_name in required else None
        signature_parameters.append(inspect.Parameter(
            field_name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=default,
            annotation=annotation,
        ))
    annotations["return"] = str
    call_tool.__annotations__ = annotations
    call_tool.__signature__ = inspect.Signature(signature_parameters, return_annotation=str)
    return FunctionTool(call_tool, name=name, description=description)



# --- Gemini model client and analytics agent ---
class Agent:
    """Creates a per-request AutoGen AssistantAgent backed by the Gemini API."""

    def __init__(self):
        self.model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

    # --- Configure the Gemini OpenAI-compatible client ---
    def _model_client(self):
        # Gemini exposes an OpenAI-compatible endpoint. AutoGen requires explicit
        # model capabilities for non-OpenAI model IDs, including tool support.
        client = OpenAIChatCompletionClient(
            model=self.model,
            api_key=os.environ["GEMINI_API_KEY"],
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            timeout=45.0,
            max_retries=0,
            temperature=0.1,
            parallel_tool_calls=False,
            model_info={
                "vision": False,
                "function_calling": True,
                "json_output": False,
                "family": "unknown",
                "structured_output": False,
            },
        )
        # AutoGen's OpenAI message type keeps the function call but does not
        # represent Gemini's provider-specific thought signature. Gemini 3.x
        # requires the signature to be echoed with the assistant tool call on
        # the next turn. Preserve it at the transport boundary, where Gemini's
        # extra_content field is still available.
        self._preserve_gemini_tool_signatures(client)
        return client

    # --- Preserve Gemini metadata across tool-call turns ---
    @staticmethod
    def _preserve_gemini_tool_signatures(client):
        signatures: dict[str, str] = {}
        completions = client._client.chat.completions
        original_create = completions.create

        async def create_with_gemini_signatures(*args, **kwargs):
            request = dict(kwargs)
            messages = request.get("messages")
            if messages:
                forwarded = []
                for message in messages:
                    if not isinstance(message, dict):
                        forwarded.append(message)
                        continue
                    message = dict(message)
                    if message.get("role") == "assistant":
                        tool_calls = message.get("tool_calls") or []
                        signature = next((
                            signatures.get(call.get("id"))
                            for call in tool_calls
                            if isinstance(call, dict) and signatures.get(call.get("id"))
                        ), None)
                        if signature:
                            message["extra_content"] = {
                                "google": {"thought_signature": signature}
                            }
                    forwarded.append(message)
                request["messages"] = forwarded

            response = await original_create(*args, **request)
            for choice in getattr(response, "choices", []):
                message = getattr(choice, "message", None)
                if message is None:
                    continue
                extra = getattr(message, "extra_content", None)
                if extra is None:
                    extra = getattr(message, "model_extra", None) or {}
                    extra = extra.get("extra_content", extra) if isinstance(extra, dict) else {}
                google = extra.get("google", {}) if isinstance(extra, dict) else {}
                message_signature = google.get("thought_signature") if isinstance(google, dict) else None
                for tool_call in getattr(message, "tool_calls", None) or []:
                    call_id = getattr(tool_call, "id", None)
                    call_extra = getattr(tool_call, "extra_content", None)
                    if call_extra is None:
                        call_extra = getattr(tool_call, "model_extra", None) or {}
                        call_extra = call_extra.get("extra_content", call_extra) if isinstance(call_extra, dict) else {}
                    call_google = call_extra.get("google", {}) if isinstance(call_extra, dict) else {}
                    signature = call_google.get("thought_signature") if isinstance(call_google, dict) else None
                    if call_id and (signature or message_signature):
                        signatures[call_id] = signature or message_signature
            return response

        completions.create = create_with_gemini_signatures

    # --- Run an analysis and record tool activity ---
    async def run(self, question, history, connections, on_event, uploaded_dataset=None):
        if uploaded_dataset is None:
            raise ValueError("Upload a CSV dataset to this investigation before asking data questions.")
        specs = await connections.tools()
        specs.append(uploaded_dataset.tool())
        log.info("AutoGen discovered %d tools", len(specs))

        charts = []
        query_results = []

        async def invoke_tool(name: str, arguments: dict[str, Any]) -> str:
            spec = next((item for item in specs if item["function"]["name"] == name), None)
            description = spec["function"].get("description", "") if spec else ""
            server = description.split("]", 1)[0].removeprefix("[") if description.startswith("[") else "MCP"
            tool_started = time.monotonic()
            await on_event({"tool": name, "server": server, "arguments": arguments, "status": "running"})
            try:
                result = uploaded_dataset.query(arguments.get("sql", "")) if name == "query_uploaded_dataset" and uploaded_dataset else await connections.call(name, arguments)
                if name == "query_uploaded_dataset":
                    query_results.append({**result, "rows": result.get("rows", [])[:100], "preview_limited": len(result.get("rows", [])) > 100, "sql": arguments.get("sql", "")})
                if name.startswith("generate_"):
                    charts.append(result)
                await on_event({"tool": name, "server": server, "arguments": arguments, "status": "completed", "summary": _summary(result), "duration_ms": round((time.monotonic() - tool_started) * 1000)})
                return json.dumps(result, default=str)[:50000]
            except Exception as exc:
                log.exception("AutoGen tool failed: %s", name)
                await on_event({"tool": name, "server": server, "arguments": arguments, "status": "failed", "summary": str(exc)[:300], "duration_ms": round((time.monotonic() - tool_started) * 1000)})
                return json.dumps({"error": str(exc)})

        tools = [_make_autogen_tool(spec, invoke_tool) for spec in specs]
        client = self._model_client()
        try:
            agent = AssistantAgent(
                name="data_detective",
                model_client=client,
                tools=tools,
                system_message=SYSTEM,
                max_tool_iterations=12,
                reflect_on_tool_use=True,
            )
            previous = "\n".join(f"{item['role']}: {item['content']}" for item in history[-12:])
            task = f"Recent conversation for context:\n{previous}\n\nCurrent question:\n{question}" if previous else question
            result = await agent.run(task=task)
            answer = getattr(result.messages[-1], "content", "") if result.messages else ""
            if not isinstance(answer, str):
                answer = str(answer)
            return {"answer": answer or "I couldn't form a reliable answer from the available data.", "charts": charts, "query_results": query_results[-1:]}
        finally:
            await client.close()



# --- Build concise execution trace summaries ---
def _summary(result):
    if isinstance(result, dict):
        if "rows" in result:
            return f"{result.get('row_count', len(result['rows']))} rows returned" + (" (truncated)" if result.get("truncated") else "")
        if "tables" in result:
            return f"{len(result['tables'])} tables inspected"
        return json.dumps(result, default=str)[:180]
    return str(result)[:180]
