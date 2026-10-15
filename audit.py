"""Read-only evidence collection. Absence on a public profile is not proof of absence."""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import json
import platform
import urllib.request

from github_client import APIError, SafetyError, command, redact

ACHIEVEMENTS = ("Quickdraw", "Starstruck", "YOLO", "Pull Shark", "Pair Extraordinaire", "Galaxy Brain", "Public Sponsor")
SLUGS = {"Quickdraw": "quickdraw", "Starstruck": "starstruck", "YOLO": "yolo",
         "Pull Shark": "pull-shark", "Pair Extraordinaire": "pair-extraordinaire",
         "Galaxy Brain": "galaxy-brain", "Public Sponsor": "public-sponsor"}
PROFILE = "https://github.com/deuxdoom?tab=achievements"


def now():
    return datetime.now(timezone.utc).isoformat()


class BadgeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        alt, src = attrs.get("alt", ""), attrs.get("src", "")
        if tag != "img" or not alt.startswith("Achievement: "):
            return
        name = alt.removeprefix("Achievement: ")
        if name not in SLUGS or not src.startswith("https://github.githubassets.com/"):
            return
        tier = "UNKNOWN"
        for value in ("bronze", "silver", "gold", "default"):
            if f"{SLUGS[name]}-{value}" in src:
                tier = "BASE" if value == "default" else value.upper()
        self.found[name] = {"status": "VERIFIED", "tier": tier, "evidence": PROFILE,
                            "asset": src, "verification": "PUBLIC_PROFILE_IMAGE"}


def profile_badges(fetcher=None):
    def fetch():
        request = urllib.request.Request(PROFILE, headers={"User-Agent": "github-badge-audit/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.geturl().split("?")[0] != "https://github.com/deuxdoom":
                raise SafetyError("프로필 조회가 예상하지 않은 URL로 이동했습니다.")
            return response.read(4_000_000).decode("utf-8")
    try:
        html = (fetcher or fetch)()
        parser = BadgeParser()
        parser.feed(html)
        return {"url": PROFILE, "observed_at": now(), "sha256": hashlib.sha256(html.encode()).hexdigest(),
                "badges": parser.found, "complete": False,
                "note": "공개 표시만 확인. 숨긴 업적과 비공개 기여는 판별 불가; 미표시는 UNKNOWN."}
    except (OSError, ValueError, SafetyError) as exc:
        return {"url": PROFILE, "observed_at": now(), "badges": {}, "complete": False,
                "error": type(exc).__name__, "note": "브라우저에서 로그인 후 업적 상세를 직접 확인하세요."}


def environment(runner=command):
    output = {"python": platform.python_version(), "compatible_minimum": "3.13", "tools": {}}
    for name, args in {"git": ["git", "--version"], "gh": ["gh", "--version"],
                       "pwsh": ["pwsh", "--version"],
                       "gh_auth": ["gh", "auth", "status", "--hostname", "github.com"]}.items():
        try:
            result = runner(args)
            text = redact(result.stdout + result.stderr)
            output["tools"][name] = {"ok": result.returncode == 0,
                                     "summary": "\n".join(line for line in text.splitlines()
                                                           if "Token:" not in line)}
        except OSError:
            output["tools"][name] = {"ok": False, "summary": "NOT_INSTALLED"}
    return output


def discussion_search(client, query, limit=5):
    gql = '''query { search(query: %s, type: DISCUSSION, first: %d) {
      discussionCount nodes { ... on Discussion { url title body isAnswered
        category { name isAnswerable } answer { url author { login } }
      } } } }''' % (json.dumps(query), limit)
    result = client.graphql(gql)["search"]
    for node in result["nodes"]:
        if node:
            node["body"] = redact(node["body"])
    return {"query": query, "total_count": result["discussionCount"],
            "sample": result["nodes"], "complete": result["discussionCount"] <= limit}


def collect(client, *, fetcher=None, env=None):
    client.assert_account()  # Fail before inspecting any other account data.
    result = {"observed_at": now(), "account": "deuxdoom", "environment": env or environment(),
              "warnings": [], "profile": profile_badges(fetcher), "achievements": {}}
    result["rate_limit"] = client.get("rate_limit")["resources"]
    for name in ACHIEVEMENTS:
        if name in ("Quickdraw", "Starstruck"):
            result["achievements"][name] = {"status": "SKIPPED", "current_status": "EARNED",
                "tier": "NOT_TARGETED", "verification": "USER_CONFIRMED", "evidence": "todo.md §0",
                "next_action": "SKIP"}
        else:
            result["achievements"][name] = result["profile"]["badges"].get(name, {
                "status": "UNKNOWN", "tier": "UNKNOWN", "verification": "NOT_DISPLAYED",
                "evidence": PROFILE, "next_action": "로그인한 프로필 상세에서 확인"})
    repos, complete = client.pages("user/repos?affiliation=owner&sort=updated")
    result["repositories"] = {"complete": complete, "items": [{
        key: repo[key] for key in ("full_name", "private", "has_discussions", "default_branch")
    } for repo in repos if repo["owner"]["login"] == "deuxdoom"]}
    merged = client.get("search/issues?q=is%3Apr%20is%3Amerged%20author%3Adeuxdoom&per_page=100")
    result["merged_prs"] = {"total_count": merged["total_count"],
        "complete": not merged.get("incomplete_results", True) and merged["total_count"] <= 100,
        "scope": "인증 계정에서 검색 가능한 PR; 검색 색인/접근 권한 제한 가능",
        "items": [], "coauthored_prs_in_sample": 0}
    for item in merged["items"][:20]:
        repo = item["repository_url"].split("/repos/", 1)[1]
        number = item["number"]
        detail = client.get(f"repos/{repo}/pulls/{number}")
        commits, commits_complete = client.pages(f"repos/{repo}/pulls/{number}/commits", max_pages=3)
        reviews, reviews_complete = client.pages(f"repos/{repo}/pulls/{number}/reviews", max_pages=3)
        has_trailer = any("co-authored-by:" in commit["commit"]["message"].lower() for commit in commits)
        result["merged_prs"]["coauthored_prs_in_sample"] += int(has_trailer)
        result["merged_prs"]["items"].append({"url": detail["html_url"], "merged": detail["merged"],
            "merged_at": detail["merged_at"], "coauthor_trailer_present": has_trailer,
            "review_count": len(reviews), "sample_complete": commits_complete and reviews_complete,
            "note": "PR 이력은 업적 표시 자체의 증거가 아님; 현재 리뷰 수는 병합 당시 상태를 보장하지 않음"})
    try:
        result["discussion_history"] = discussion_search(client, "commenter:deuxdoom is:answered", 10)
        result["discussion_history"]["accepted_answers_by_account_in_sample"] = sum(
            1 for node in result["discussion_history"]["sample"]
            if ((node.get("answer") or {}).get("author") or {}).get("login") == "deuxdoom")
        result["discussion_candidates"] = discussion_search(client,
            "repo:community/community is:unanswered PowerShell sort:updated-desc", 5)
    except (APIError, SafetyError) as exc:
        result["warnings"].append(str(exc))
    repo = client.get("repos/deuxdoom/github-badge")
    branches, branches_complete = client.pages("repos/deuxdoom/github-badge/branches")
    result["publication_target"] = {"repo": repo["full_name"], "default_branch": repo["default_branch"],
        "private": repo["private"], "push": repo.get("permissions", {}).get("push", False),
        "branches": branches, "branches_complete": branches_complete,
        "rulesets": client.get("repos/deuxdoom/github-badge/rulesets?includes_parents=true")}
    return result
