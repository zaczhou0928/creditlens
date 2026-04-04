# In-process MCP runtime

The SQL agent discovers and calls structured tools through an in-process MCP server. Tools provide authorized schema search, read-only DuckDB queries, date utilities, and chart specifications; no additional listening port is required.

Each client is bound to a server-side access scope. Query connections contain only authorized rows and columns. Results carry SQL, exact values, column types, truncation status, data version, and cutoff date.

The model may inspect data or repair SQL within its six-call budget. Reports use server-owned result IDs and validated chart sources. Decimal report calculations are local agent operations, not a separate MCP service.

Demo mode uses the metric compiler and template reports. It exercises the shared execution boundaries without claiming to reproduce live-model behavior. Authenticated users can inspect the available contracts at `/api/mcp/tools`.
