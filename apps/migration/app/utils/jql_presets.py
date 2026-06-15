"""JQL preset templates for the migration UI."""

from __future__ import annotations


def _parse_issue_types(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _project_clause(project_key: str) -> str:
    key = (project_key or "").strip() or "KEY"
    return f"project = {key}"


def build_jql_presets(project_key: str, issue_type_filter: str = "") -> list[dict]:
    """Return preset JQL queries for the migration picker."""
    proj = _project_clause(project_key)
    presets: list[dict] = [
        {
            "id": "all_asc",
            "label": "Tüm issue'lar (eskiden yeniye)",
            "jql": f"{proj} ORDER BY created ASC",
        },
        {
            "id": "all_desc",
            "label": "Tüm issue'lar (yeniden eskiye)",
            "jql": f"{proj} ORDER BY created DESC",
        },
        {
            "id": "no_subtasks",
            "label": "Ana tasklar (Sub-task hariç)",
            "jql": f"{proj} AND issuetype NOT IN (Sub-task) ORDER BY created ASC",
        },
        {
            "id": "open_only",
            "label": "Sadece açık issue'lar",
            "jql": f"{proj} AND statusCategory != Done ORDER BY created ASC",
        },
        {
            "id": "done_only",
            "label": "Sadece tamamlananlar",
            "jql": f"{proj} AND statusCategory = Done ORDER BY created DESC",
        },
        {
            "id": "updated_30d",
            "label": "Son 30 günde güncellenenler",
            "jql": f"{proj} AND updated >= -30d ORDER BY updated DESC",
        },
        {
            "id": "by_priority",
            "label": "Önceliğe göre (yüksek → düşük)",
            "jql": f"{proj} ORDER BY priority DESC",
        },
        {
            "id": "by_key",
            "label": "Key sırasına göre",
            "jql": f"{proj} ORDER BY key ASC",
        },
    ]

    issue_types = _parse_issue_types(issue_type_filter)
    if issue_types:
        types_jql = ", ".join(f'"{t}"' for t in issue_types)
        presets.append({
            "id": "by_issue_types",
            "label": f"Ayarlardaki issue tipleri ({len(issue_types)} tip)",
            "jql": f"{proj} AND issuetype in ({types_jql}) ORDER BY created ASC",
        })

    return presets
