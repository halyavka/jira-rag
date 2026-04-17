"""jira-rag-client — thin HTTP wrapper for the jira-rag service.

Public API:
    >>> from jira_rag_client import JiraRagClient
    >>> client = JiraRagClient()   # reads JIRA_RAG_URL from env
    >>> issue = client.get_issue("PROJ-123")
    >>> hits = client.search("password reset flow", project_keys=["PROJ"])

Module-level convenience functions (mirror the legacy dict-returning API that
predates this package, so existing code migrates without changes):

    >>> from jira_rag_client import get_issue_context, find_related_tasks
    >>> get_issue_context("PROJ-123")      # -> dict
    >>> find_related_tasks("password flow") # -> list[dict]
"""

from jira_rag_client.client import (
    Comment,
    IssueContext,
    JiraRagClient,
    JiraRagError,
    MergeRequest,
    SearchHit,
    find_related_tasks,
    get_issue_context,
    health_check,
)
from jira_rag_client.formatters import (
    format_issue_for_prompt,
    format_related_tasks_for_prompt,
)

__version__ = "0.1.0"

__all__ = [
    "JiraRagClient",
    "JiraRagError",
    "IssueContext",
    "SearchHit",
    "Comment",
    "MergeRequest",
    "get_issue_context",
    "find_related_tasks",
    "health_check",
    "format_issue_for_prompt",
    "format_related_tasks_for_prompt",
    "__version__",
]
