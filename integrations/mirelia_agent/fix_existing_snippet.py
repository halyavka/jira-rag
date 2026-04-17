"""
Optional enhancement for mirelia-agent/nodes/fix_existing.py:
semantic search for related tasks when --jira wasn't provided.

Requires the jira-rag-client package:
    pip install "git+https://github.com/halyavka/jira-rag.git#subdirectory=integrations/jira_rag_client"

Paste near the top of the fix prompt builder in nodes/fix_existing.py, right
after stack_trace / failed_selector / error_message are available on `state`.

Runs only when jira_context is empty (user didn't pass --jira) and the
failure has usable signal. Costs one HTTP call to jira-rag (~50 ms).
"""

from jira_rag_client import JiraRagClient, format_related_tasks_for_prompt

_RAG = JiraRagClient()   # reads JIRA_RAG_URL from env


def build_related_tasks_context(state: dict) -> str:
    """Pick the best-shaped query from failure signal, search jira-rag."""
    if state.get("jira_context"):
        return ""   # user already gave us a ticket

    # Compose a query from whatever signal we have. The test method name is
    # the strongest signal because mirelia uses descriptive names like
    # `registerUserViaSmsAndVerifyEmail`.
    parts = [
        state.get("test_method", ""),
        state.get("page_object_class", ""),
        state.get("api_service", ""),
        state.get("error_message", "")[:200],
    ]
    query = " ".join(p for p in parts if p).strip()
    if not query:
        return ""

    hits = _RAG.search(
        query,
        project_keys=["PID"],     # mirelia project key
        top_k=2,
        min_score=0.5,            # conservative — only inject strong matches
    )
    return format_related_tasks_for_prompt(hits)
