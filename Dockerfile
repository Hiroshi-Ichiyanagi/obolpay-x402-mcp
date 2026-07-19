# Dockerfile for Glama MCP introspection (and general container use).
# The stdio MCP server starts and answers tools/list without any secrets;
# X402_AGENT_PRIVATE_KEY is only needed at runtime for purchase()/spend_gasless().
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY x402_mcp_server.py .
CMD ["python", "x402_mcp_server.py"]
