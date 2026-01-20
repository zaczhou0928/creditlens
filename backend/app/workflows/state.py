from typing import TypedDict, Any


class QueryState(TypedDict, total=False):
    task_id: str
    query: str
    session_id: str
    access_scope: dict[str, Any]
    previous_plan: dict[str, Any] | None
    recent_context: str
    knowledge: dict[str, Any]
    agent_trace: list[dict[str, Any]]
    decision: dict[str, Any]
    confirmed: dict[str, str]
    plan: dict[str, Any]
    sources: list[dict[str, Any]]
    workspace: dict[str, Any]
    clarification: dict[str, Any] | None
    retrieval: dict[str, Any]
    schema_graph: dict[str, Any]
    result: dict[str, Any]
