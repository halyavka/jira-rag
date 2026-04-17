# jira-rag-client

Thin Python client for the [jira-rag](https://github.com/halyavka/jira-rag)
service. Gives agents two operations:

- **Semantic search** — "find the Jira task that describes functionality X"
- **Full context hydration** — "give me everything about PROJ-123:
  description + comments + linked MRs + status history"

Stdlib-only (no `requests` / `httpx`), typed dataclasses, drop-in replacement
for the pre-package copy-paste helpers.

## Install

This package lives inside the `jira-rag` monorepo under
`integrations/jira_rag_client/`. Install it via pip's `#subdirectory=`
directive (quote the URL — `#` is a shell comment char):

```bash
pip install "git+https://github.com/halyavka/jira-rag.git#subdirectory=integrations/jira_rag_client"
```

Pin a tag / commit for reproducibility:
```bash
pip install "git+https://github.com/halyavka/jira-rag.git@v0.1.0#subdirectory=integrations/jira_rag_client"
```

Or from a local checkout during development:
```bash
pip install -e /path/to/jira-rag/integrations/jira_rag_client
```

Add to your project's `pyproject.toml`:
```toml
[project]
dependencies = [
    "jira-rag-client @ git+https://github.com/halyavka/jira-rag.git#subdirectory=integrations/jira_rag_client",
]
```

In `requirements.txt`:
```
jira-rag-client @ git+https://github.com/halyavka/jira-rag.git#subdirectory=integrations/jira_rag_client
```

## Configuration

The client reads two env vars (both optional):

| Var | Default | Purpose |
|---|---|---|
| `JIRA_RAG_URL` | `http://localhost:8100` | Base URL of the jira-rag HTTP service |
| `JIRA_RAG_TIMEOUT` | `5` | Request timeout in seconds |

Or pass them explicitly:
```python
client = JiraRagClient(base_url="http://jira-rag:8100", timeout=10)
```

## Usage

### Typed API (recommended)

```python
from jira_rag_client import JiraRagClient, format_issue_for_prompt

client = JiraRagClient()

# Known ticket → full context
issue = client.get_issue("PROJ-4275")
if issue:
    print(issue.status, issue.progress_percent)
    for c in issue.comments:
        print(c.author, c.body_text[:80])
    prompt_section = format_issue_for_prompt(issue)

# Unknown ticket → semantic search
hits = client.search(
    "user can reset password via SMS",
    project_keys=["PROJ"],
    top_k=5,
    min_score=0.4,
)
for hit in hits:
    print(hit.issue_key, hit.score, hit.context.summary)
```

### Dict API (backward-compatible)

```python
from jira_rag_client import get_issue_context, find_related_tasks

# Same shape as the jira-rag HTTP response — no dataclass knowledge needed.
issue: dict = get_issue_context("PROJ-4275")
hits: list[dict] = find_related_tasks("password flow", project_keys=["PROJ"])
```

### Prompt formatting

```python
from jira_rag_client import (
    JiraRagClient,
    format_issue_for_prompt,
    format_related_tasks_for_prompt,
)

client = JiraRagClient()
issue = client.get_issue("PROJ-4275")

prompt = f"""
{format_issue_for_prompt(issue, max_description_chars=2500, max_comments=3)}

Now fix the failing test described in the stack trace below.
<... agent-specific instructions ...>
"""
```

Formatters accept both typed dataclasses and plain dicts — whichever your
code paths produce.

## Error handling

By default calls **never raise** — they return `None` / `[]` / `{}` on any
failure (network, timeout, 5xx, malformed JSON). That's the right default
for agent flows: a dead jira-rag shouldn't crash your fix command.

Opt in to exceptions when you need them:
```python
client = JiraRagClient(raise_on_error=True)
try:
    issue = client.get_issue("PROJ-4275")
except JiraRagError as e:
    ...
```

`health_check()` is always safe — returns `bool`, never raises:
```python
if not client.health_check():
    log.warning("jira-rag unreachable, proceeding without Jira context")
```

## Why HTTP (not a Python SDK into the indexer)

The jira-rag service owns Qdrant + Supabase + embedding-model lifecycle. If
this client spoke to Qdrant directly it would drag qdrant-client, fastembed,
and psycopg2 into every consumer. HTTP keeps the service boundary clean:
restart jira-rag, upgrade embeddings, swap the vector DB — consumers don't
care.

## Compatibility

- Python 3.9+
- jira-rag server 0.1.x

## Development

```bash
pip install -e '.[dev]'
pytest
```
