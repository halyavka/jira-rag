"""Detect query intent — what kind of filter to apply at retrieval time.

Intent flags produced:
- ``bug_intent``  — user is asking about bugs / problems / things that broke
- ``open_intent`` — user is asking about current / unresolved state

Heuristic (keyword-based) as v1. Cheap, transparent, and easy to eval. If
evaluation reveals missed cases, swap in a Haiku-based classifier without
changing callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Mixed UA/EN/RU vocabulary — PID tickets and users both use the lot.
_BUG_KEYWORDS = [
    # English
    r"\bbug(s|gy)?\b",
    r"\bissues?\b",
    r"\bproblems?\b",
    r"\bbroken\b",
    r"\bfail(ures?|ing)?\b",
    r"\berrors?\b",
    r"\bdefects?\b",
    r"\bincidents?\b",
    r"\bnot working\b",
    # Ukrainian
    r"\bбаг(и|а|у|і|ів)?\b",
    r"\bпроблем(а|и|у|і|ами|ах|ою)?\b",
    r"\bпомилк(а|и|у|і|ою|ами|ах)?\b",
    r"\bзламан(о|ий|а|е|ий|і)\b",
    r"\bне прац(ює|юють)\b",
    r"\bфейл(и|ів|у|ом|ами)?\b",
    # Russian
    r"\bбаг(и|а|у|и|ам|ах)?\b",
    r"\bошибк(а|и|у|ой|ами)?\b",
    r"\bпроблем(а|ы|у|ой|ами)?\b",
    r"\bне работает\b",
    r"\bсломан(о|ый|а|ые)\b",
]
_OPEN_KEYWORDS = [
    # English
    r"\bopen\b",
    r"\bunresolved\b",
    r"\bcurrently\b",
    r"\bactive\b",
    r"\bin progress\b",
    r"\bpending\b",
    r"\bongoing\b",
    r"\btodo\b",
    r"\bwhat'?s? (wrong|broken|left)\b",
    # Ukrainian — only explicit "open state" words. "зараз" is too filler,
    # don't include (users say "баги зараз" meaning just "bugs", not
    # "currently-open bugs").
    r"\bвідкрит(і|ий|а|е|их)\b",
    r"\bактивн(і|ий|а|е)\b",
    r"\bв роботі\b",
    r"\bв процесі\b",
    r"\bнезавершен(і|ий|а|е)\b",
    r"\bщо (зламано|не працює|треба|залишилось)\b",
    # Russian
    r"\bактивн(ы|ый|ая|ое|ые)\b",
    r"\bв работе\b",
    r"\bоткрыт(ы|ый|ая|ое|ые)\b",
]


_BUG_RE = re.compile("|".join(_BUG_KEYWORDS), re.IGNORECASE)
_OPEN_RE = re.compile("|".join(_OPEN_KEYWORDS), re.IGNORECASE)

# Issue types that indicate the ticket itself is a bug record.
BUG_ISSUE_TYPES = ["Bug"]

# Jira status categories that indicate an issue is NOT completed.
OPEN_STATUS_CATEGORIES = ["new", "indeterminate"]


@dataclass
class QueryIntent:
    bug_intent: bool = False
    open_intent: bool = False

    @property
    def must_issue_types(self) -> list[str] | None:
        return BUG_ISSUE_TYPES if self.bug_intent else None

    @property
    def must_status_categories(self) -> list[str] | None:
        if self.bug_intent and not self.open_intent:
            # Bug queries pick up closed bugs too (product history matters) — don't force open.
            return None
        if self.open_intent:
            return OPEN_STATUS_CATEGORIES
        return None


def classify(query: str) -> QueryIntent:
    if not query:
        return QueryIntent()
    return QueryIntent(
        bug_intent=bool(_BUG_RE.search(query)),
        open_intent=bool(_OPEN_RE.search(query)),
    )
