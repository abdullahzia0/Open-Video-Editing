import pytest

from ove.mcp.server import create_server


@pytest.mark.asyncio
async def test_mcp_tools_and_truthful_error(service):
    server = create_server(service)
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert {"plans_create", "render_submit", "jobs_get", "designs_execute"} <= names
    result = await server.call_tool("designs_execute", {"action": "upload", "arguments": {}})
    # FastMCP returns a protocol result directly for tools with custom result envelopes.
    assert result.isError
    assert result.structuredContent["error"]["code"] == "provider_unavailable"
    schemas = [tool.inputSchema for tool in tools if tool.name == "plans_create"]
    assert "request" in schemas[0]["properties"]


@pytest.mark.asyncio
async def test_real_stdio_protocol(tmp_path):
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    environment = {**os.environ, "OVE_DATA_DIR": str(tmp_path / "stdio-data")}
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ove.cli.main", "serve"],
        env=environment,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Open Video Editing"
            tools = await session.list_tools()
            assert len(tools.tools) == 20
            response = await session.call_tool("connections_status", {})
            assert not response.isError
            assert response.structuredContent["data"]["available"] is False
            failed = await session.call_tool(
                "designs_execute", {"action": "upload", "arguments": {}}
            )
            assert failed.isError
