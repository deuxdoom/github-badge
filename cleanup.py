"""Delete only this run's exact, merged, unchanged source branch; never repositories."""
from __future__ import annotations

from urllib.parse import quote

from github_client import SafetyError


def cleanup(publisher):
    state, plan, client = publisher.state, publisher.plan, publisher.client
    if state.get("branch_cleaned"):
        return
    if not state.get("branch_created") or not state.get("pr_number"):
        return
    client.assert_account()
    prefix = f"repos/{plan['repo']}"
    pr = client.get(prefix + f"/pulls/{state['pr_number']}")
    if (not pr["merged"] or pr["head"]["ref"] != plan["branch"] or pr["base"]["ref"] != plan["base"]
            or pr["head"]["repo"]["full_name"] != plan["repo"]
            or pr["head"]["sha"] != state["commit_sha"] or publisher.marker not in (pr.get("body") or "")):
        raise SafetyError("정리 거부: 이번 실행의 병합 완료 PR/브랜치를 확인할 수 없습니다.")
    endpoint = prefix + "/git/refs/heads/" + plan["branch"]
    ref = client.optional(prefix + "/git/ref/heads/" + plan["branch"])
    if ref is None:
        # GitHub's own delete_branch_on_merge setting may already have removed it.
        if state.get("pending") not in (None, "cleanup"):
            raise SafetyError("다른 쓰기 요청이 미확정 상태입니다.")
        state.update(branch_cleaned=True, pending=None)
        publisher.save()
        return
    if ref["object"]["sha"] != state["commit_sha"]:
        raise SafetyError("정리 거부: 브랜치에 다른 변경이 추가되었습니다.")
    query = quote("deuxdoom:" + plan["branch"], safe="")
    open_prs = client.get(prefix + f"/pulls?state=open&head={query}&per_page=100")
    if open_prs:
        raise SafetyError("정리 거부: 작업 브랜치를 사용하는 열린 PR이 있습니다.")
    publisher.mutate("cleanup", "DELETE", endpoint, None, "branch_cleaned", lambda _: True)
    if client.optional(prefix + "/git/ref/heads/" + plan["branch"]) is not None:
        raise SafetyError("정리 후 브랜치 삭제를 확인하지 못했습니다.")
