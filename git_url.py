"""Helpers for parsing remote Git URLs."""

from __future__ import annotations

import re


def parse_git_url(git_url: str) -> tuple[str, str, str]:
    """Parse remote Git URL into (owner, repo, slug)."""
    if not git_url:
        return "", "", ""
    cleaned = re.sub(r"\.git$", "", git_url.strip().rstrip("/"))
    cleaned = re.sub(r"^[a-zA-Z]+://", "", cleaned)
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[1]
        if ":" in cleaned:
            cleaned = cleaned.split(":", 1)[1]
        elif "/" in cleaned:
            cleaned = cleaned.split("/", 1)[1]
    elif "/" in cleaned:
        first_part = cleaned.split("/", 1)[0]
        if "." in first_part or ":" in first_part:
            cleaned = cleaned.split("/", 1)[1]

    parts = [p for p in cleaned.split("/") if p]
    repo = parts[-1] if len(parts) >= 1 else ""
    owner = parts[-2] if len(parts) >= 2 else ""

    repo = re.sub(r"[^a-zA-Z0-9_\-.]", "", repo).strip(".-")
    owner = re.sub(r"[^a-zA-Z0-9_\-.]", "", owner).strip(".-")

    if owner and repo:
        slug = f"{owner}-{repo}".lower()
    elif repo:
        slug = repo.lower()
    else:
        slug = "project"

    slug = re.sub(r"[^a-zA-Z0-9_\-]", "-", slug).strip("-")
    return owner, repo, slug
