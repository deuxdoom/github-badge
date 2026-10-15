"""A single useful source-publication PR, never an achievement farming loop."""
from __future__ import annotations

import base64
import hashlib
import uuid
from urllib.parse import quote

from audit import now
from github_client import APIError, SafetyError
from storage import ROOT, STATE, digest, load, save

SOURCE_FILES = (".gitignore", "README.md", "README.ko.md", "run.py", "audit.py", "achievements.py",
                "github_client.py", "cleanup.py", "storage.py", "config.example.json",
                "tests/__init__.py", "tests/test_automation.py")
BOOTSTRAP = "# GitHub Badge Audit\n\nRead-only achievement audit and an explicitly approved source publication workflow.\n"
TITLE = "feat: add safe GitHub achievement audit and orchestration CLI"


def make_plan(audit, config=None):
    config = config or {}
    if config.get("account", "deuxdoom") != "deuxdoom" or config.get("repo", "deuxdoom/github-badge") != "deuxdoom/github-badge":
        raise SafetyError("이 배포는 deuxdoom/github-badge 전용입니다.")
    confirmed = config.get("user_confirmed_earned", [])
    if not isinstance(confirmed, list) or any(name not in audit["achievements"] for name in confirmed):
        raise SafetyError("user_confirmed_earned 값이 잘못되었습니다.")
    for name in confirmed:
        audit["achievements"][name].update(status="SKIPPED", current_status="EARNED",
                                           verification="USER_CONFIRMED", next_action="SKIP")
    target = audit["publication_target"]
    reasons = []
    if target["branches"] or not target["branches_complete"]:
        reasons.append("TARGET_NOT_EMPTY: 기존 저장소 파일은 자동 덮어쓰지 않습니다.")
    if not target["push"]:
        reasons.append("PUSH_PERMISSION_REQUIRED")
    if target["default_branch"] != "main":
        reasons.append("UNEXPECTED_DEFAULT_BRANCH")
    if target["private"]:
        reasons.append("TARGET_VISIBILITY_CHANGED: 공개 대상만 이 계획에 포함됩니다.")
    if any(rule.get("enforcement") == "active" for rule in target["rulesets"]):
        reasons.append("ACTIVE_RULESET: 보호 규칙이 있는 저장소는 자동 병합하지 않습니다.")
    if all(audit["achievements"][name]["status"] in ("VERIFIED", "SKIPPED") for name in ("YOLO", "Pull Shark")):
        reasons.append("ALREADY_EARNED: YOLO/Pull Shark 추가 활동 생략")
    payload = []
    for name in SOURCE_FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise SafetyError(f"게시 파일 누락 또는 symlink: {name}")
        content = path.read_text(encoding="utf-8")
        payload.append({"path": name, "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()})
    run_id = uuid.uuid4().hex[:12]
    return {"schema": 1, "run_id": run_id, "created_at": now(), "account": "deuxdoom",
        "repo": "deuxdoom/github-badge", "base": "main", "branch": f"badge-tool/{run_id}",
        "title": TITLE, "bootstrap_content": BOOTSTRAP, "payload": payload,
        "user_confirmed_earned": sorted(set(confirmed) | {"Quickdraw", "Starstruck"}),
        "blocked": reasons, "max_prs": 1,
        "purpose": "사용자가 요청한 감사 CLI, 테스트, 한국어 사용법 게시",
        "badge_policy": "표시되지 않은 업적은 UNKNOWN; 병합과 업적 획득은 별도 검증. 보유 업적 재작업 금지.",
        "operations": ["빈 저장소 main에 README 최초 커밋 1건", "소스 파일을 담은 작업 브랜치/커밋 1건",
                       "실질적인 구현 PR 1건 생성", "리뷰 강제 규칙이 없고 검사에 통과한 경우 일반 merge API로 병합"],
        "cleanup": [f"이번 실행에서 만든 badge-tool/{run_id} 브랜치만, 병합 확인 및 SHA 일치 후 삭제"],
        "retained": ["저장소", "main과 구현 소스", "PR/커밋/이벤트 기록", "로컬 소스, 증빙, 체크포인트, 보고서"],
        "excluded": ["모든 다른 저장소", "이슈/Discussions/별/협업자/보호 규칙/설정 변경", "결제", "원격 저장소 삭제"]}


def preview(plan):
    summary = {key: value for key, value in plan.items() if key not in ("payload", "bootstrap_content")}
    summary["files"] = [{"path": f["path"], "sha256": f["sha256"], "bytes": len(f["content"].encode())}
                        for f in plan["payload"]]
    summary["approval_sha256"] = digest(plan)
    return summary


def validate_plan(plan):
    if plan.get("schema") != 1 or plan.get("account") != "deuxdoom" or plan.get("repo") != "deuxdoom/github-badge":
        raise SafetyError("잘못된 계획 대상입니다.")
    if plan.get("base") != "main" or plan.get("branch") != f"badge-tool/{plan.get('run_id')}":
        raise SafetyError("브랜치 계획 불일치")
    import re
    if not re.fullmatch(r"[a-f0-9]{12}", plan["run_id"]) or plan.get("max_prs") != 1:
        raise SafetyError("잘못된 실행 식별자/PR 수")
    if [f["path"] for f in plan["payload"]] != list(SOURCE_FILES):
        raise SafetyError("게시 경로 화이트리스트 불일치")
    for entry in plan["payload"]:
        if hashlib.sha256(entry["content"].encode()).hexdigest() != entry["sha256"]:
            raise SafetyError("승인할 소스 해시 불일치")


def verify_local_bundle(plan):
    validate_plan(plan)
    for entry in plan["payload"]:
        path = ROOT / entry["path"]
        if path.is_symlink() or path.read_text(encoding="utf-8") != entry["content"]:
            raise SafetyError(f"계획 작성 후 소스 변경: {entry['path']}. --dry-run으로 다시 검토하세요.")


class Publisher:
    def __init__(self, client, plan, state, persist=None):
        validate_plan(plan)
        if state.get("approved_digest") != digest(plan):
            raise SafetyError("이 계획에 대한 승인이 없습니다.")
        self.client, self.plan, self.state = client, plan, state
        self.persist = persist or (lambda value: save(STATE / "checkpoint.json", value))
        self.prefix = f"repos/{plan['repo']}"
        self.marker = f"<!-- github-badge-run:{plan['run_id']} -->"

    def save(self):
        self.persist(self.state)

    def recover(self, field, value, op):
        if self.state.get("pending") != op:
            raise SafetyError(f"추적되지 않은 기존 자원: {op}")
        self.state[field] = value
        self.state["pending"] = None
        self.save()

    def mutate(self, op, method, endpoint, payload, field, extract=lambda result: result, *, immutable=False):
        pending = self.state.get("pending")
        if pending and not (pending == op and immutable):
            raise SafetyError(f"미확정 쓰기 요청 {pending}: 원격 반영 확인 후 재개해야 합니다.")
        self.state["pending"] = op
        self.save()
        result = self.client.write(method, endpoint, payload)
        self.state[field] = extract(result)
        self.state["pending"] = None
        self.save()
        return self.state[field]

    def check_target(self):
        self.client.assert_account()
        repo = self.client.get(self.prefix)
        if repo["full_name"] != self.plan["repo"] or repo["default_branch"] != "main" or repo["private"]:
            raise SafetyError("승인 이후 저장소 대상/가시성/기본 브랜치가 변경되었습니다.")
        if repo.get("archived") or not repo.get("permissions", {}).get("push"):
            raise SafetyError("쓰기 가능한 저장소가 아닙니다.")
        if not repo.get("allow_merge_commit", True):
            raise SafetyError("일반 merge commit을 허용하지 않는 저장소입니다.")
        rules, complete = self.client.pages(self.prefix + "/rulesets?includes_parents=true")
        if not complete or any(rule.get("enforcement") == "active" for rule in rules):
            raise SafetyError("보호 규칙 존재/판별 불가: 자동 병합을 중단합니다.")

    def ensure_base(self):
        main = self.client.optional(self.prefix + "/git/ref/heads/main", statuses=(404, 409))
        if self.state.get("base_sha"):
            return self.state["base_sha"]
        if main:
            commit = self.client.get(self.prefix + "/git/commits/" + main["object"]["sha"])
            content = self.client.get(self.prefix + "/contents/README.md?ref=main")
            expected = base64.b64encode(self.plan["bootstrap_content"].encode()).decode()
            if (self.state.get("pending") != "bootstrap" or self.marker not in commit["message"]
                    or commit["parents"] or content.get("content", "").replace("\n", "") != expected):
                raise SafetyError("저장소가 비어 있지 않습니다. 기존 파일은 변경하지 않습니다.")
            self.recover("base_sha", main["object"]["sha"], "bootstrap")
        else:
            branches, complete = self.client.pages(self.prefix + "/branches")
            if branches or not complete:
                raise SafetyError("예상하지 않은 기존 브랜치가 있습니다.")
            self.mutate("bootstrap", "PUT", self.prefix + "/contents/README.md", {
                "message": f"docs: initialize audit CLI repository\n\n{self.marker}",
                "content": base64.b64encode(self.plan["bootstrap_content"].encode()).decode(),
                "branch": "main"}, "base_sha", lambda value: value["commit"]["sha"])
        return self.state["base_sha"]

    def ensure_commit(self, base):
        base_commit = self.client.get(self.prefix + "/git/commits/" + base)
        if not self.state.get("tree_sha"):
            self.mutate("tree", "POST", self.prefix + "/git/trees", {
                "base_tree": base_commit["tree"]["sha"],
                "tree": [{"path": f["path"], "mode": "100644", "type": "blob", "content": f["content"]}
                         for f in self.plan["payload"]]}, "tree_sha", lambda value: value["sha"], immutable=True)
        if not self.state.get("commit_sha"):
            # Use the authenticated user's bootstrap identity, never an invented coauthor.
            author = {key: base_commit["author"][key] for key in ("name", "email")}
            author["date"] = self.plan["created_at"]
            self.mutate("commit", "POST", self.prefix + "/git/commits", {
                "message": f"{self.plan['title']}\n\n{self.marker}", "tree": self.state["tree_sha"],
                "parents": [base], "author": author, "committer": author},
                "commit_sha", lambda value: value["sha"], immutable=True)
        return self.state["commit_sha"]

    def ensure_branch(self, sha):
        endpoint = self.prefix + "/git/ref/heads/" + self.plan["branch"]
        ref = self.client.optional(endpoint)
        if ref:
            if ref["object"]["sha"] != sha:
                raise SafetyError("작업 브랜치 SHA가 달라졌습니다. 다른 변경을 덮어쓰지 않습니다.")
            if not self.state.get("branch_created"):
                self.recover("branch_created", True, "branch")
        elif self.state.get("branch_created"):
            raise SafetyError("생성했던 작업 브랜치가 사라졌습니다.")
        else:
            self.mutate("branch", "POST", self.prefix + "/git/refs", {
                "ref": "refs/heads/" + self.plan["branch"], "sha": sha}, "branch_created", lambda _: True)

    def ensure_pr(self):
        query = quote("deuxdoom:" + self.plan["branch"], safe="")
        pulls = self.client.get(self.prefix + f"/pulls?state=all&head={query}&base=main&per_page=100")
        matches = [pr for pr in pulls if self.marker in (pr.get("body") or "")]
        if len(matches) > 1 or len(pulls) != len(matches):
            raise SafetyError("추적되지 않은 PR 또는 중복 PR이 있습니다.")
        if matches:
            pr = matches[0]
            if pr["head"]["sha"] != self.state["commit_sha"] or pr["user"]["login"] != "deuxdoom":
                raise SafetyError("기존 PR 대상/내용이 달라졌습니다.")
            if not self.state.get("pr_number"):
                self.recover("pr_number", pr["number"], "pr")
            elif self.state["pr_number"] != pr["number"]:
                raise SafetyError("체크포인트 PR 번호 불일치")
        elif self.state.get("pr_number"):
            raise SafetyError("기존 PR을 확인하지 못했습니다. 새 PR을 생성하지 않습니다.")
        else:
            self.mutate("pr", "POST", self.prefix + "/pulls", {
                "title": self.plan["title"], "head": self.plan["branch"], "base": "main",
                "body": "Add the requested standard-library CLI, read-only audit, approval-bound publication, "
                        "crash recovery, allowlisted cleanup and mock tests.\n\n"
                        "Validation: python -m unittest discover -v; python run.py --dry-run.\n\n" + self.marker},
                "pr_number", lambda value: value["number"])
        return self.state["pr_number"]

    def merge(self, number):
        endpoint = self.prefix + f"/pulls/{number}"
        pr = self.client.get(endpoint)
        if (pr["head"]["sha"] != self.state["commit_sha"] or pr["head"]["ref"] != self.plan["branch"]
                or pr["head"]["repo"]["full_name"] != self.plan["repo"] or pr["base"]["ref"] != "main"
                or pr["user"]["login"] != "deuxdoom" or self.marker not in (pr.get("body") or "")):
            raise SafetyError("PR 소유자/대상/승인 내용 불일치: 병합 또는 성공 판정을 중단합니다.")
        if pr["merged"]:
            if self.state.get("pending") == "merge":
                self.recover("merged", True, "merge")
            self.state.update(merged=True, pr_url=pr["html_url"], merge_commit_sha=pr["merge_commit_sha"])
            self.save()
            return
        if pr["state"] != "open" or pr["head"]["sha"] != self.state["commit_sha"]:
            raise SafetyError("PR 상태/내용이 변경되어 병합을 중단합니다.")
        self.check_target()
        branch = self.client.get(self.prefix + "/branches/main")
        if branch.get("protected") or branch["commit"]["sha"] != self.state["base_sha"]:
            raise SafetyError("main 보호 또는 승인 이후 변경 감지: 병합을 중단합니다.")
        rules = self.client.get(self.prefix + "/rules/branches/main")
        if rules:
            raise SafetyError("적용되는 브랜치 규칙이 있어 자동 병합을 중단합니다.")
        reviews, complete = self.client.pages(endpoint + "/reviews")
        if reviews or not complete:
            raise SafetyError("리뷰가 추가되었습니다. 리뷰 워크플로를 수동 완료하세요.")
        sha = self.state["commit_sha"]
        checks = self.client.get(self.prefix + f"/commits/{sha}/check-runs?per_page=100")
        status = self.client.get(self.prefix + f"/commits/{sha}/status")
        if (checks["total_count"] > len(checks["check_runs"]) or any(
            check["status"] != "completed" or check.get("conclusion") not in ("success", "neutral", "skipped")
            for check in checks["check_runs"]) or (status["total_count"] and status["state"] != "success")):
            raise SafetyError("검사 실패/진행 중/불완전: 자동 병합을 중단합니다.")
        if pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
            raise SafetyError("GitHub 병합 가능 여부가 아직 clean이 아닙니다. 나중에 --resume 하세요.")
        self.mutate("merge", "PUT", endpoint + "/merge", {"sha": sha, "merge_method": "merge"},
                    "merged", lambda result: result.get("merged") is True)
        if not self.state["merged"]:
            raise SafetyError("병합 성공을 확인하지 못했습니다.")
        verified = self.client.get(endpoint)
        if not verified["merged"]:
            raise SafetyError("서버에서 병합 상태를 확인하지 못했습니다.")
        self.state.update(pr_url=verified["html_url"], merge_commit_sha=verified["merge_commit_sha"])
        self.save()

    def execute(self):
        if self.plan["blocked"]:
            raise SafetyError("; ".join(self.plan["blocked"]))
        self.check_target()
        if self.state.get("pr_number"):
            # A merge or cleanup may have completed just before a crash.
            pr = self.client.get(self.prefix + f"/pulls/{self.state['pr_number']}")
            if pr["merged"]:
                self.merge(self.state["pr_number"])
                return
        base = self.ensure_base()
        main = self.client.get(self.prefix + "/git/ref/heads/main")
        if main["object"]["sha"] != base:
            raise SafetyError("main에 다른 변경이 추가되었습니다. 새 PR 게시를 중단합니다.")
        sha = self.ensure_commit(base)
        self.ensure_branch(sha)
        self.merge(self.ensure_pr())


def statuses(audit, state):
    rows = []
    for name, initial in audit["achievements"].items():
        row = {"achievement": name, "initial": initial["status"], "status": initial["status"],
               "tier": initial["tier"], "evidence": initial["evidence"], "action": "없음", "next": ""}
        if initial["status"] in ("VERIFIED", "SKIPPED"):
            row["next"] = "보유 완료, 추가 작업 생략"
        elif name == "Pair Extraordinaire":
            row.update(status="BLOCKED", next="REAL_COAUTHOR_REQUIRED: 실제 공동 작업자 기여·동의 필요")
        elif name == "Galaxy Brain":
            row.update(status="BLOCKED", next="REVIEWED_ANSWER_REQUIRED: 검증한 답변 초안 승인 및 자발적 채택 필요")
        elif name == "Public Sponsor":
            row.update(status="BLOCKED", next="PAYMENT_REQUIRED: 사용자가 직접 공개 후원·결제")
        elif state.get("merged"):
            row.update(status="PENDING", action="감사 도구 구현 PR 1건 병합", evidence=state.get("pr_url", ""),
                       next="업적 미표시: 반영 지연/숨김/기타 자격 조건 확인; 병합은 획득 보장 아님")
        else:
            row["next"] = "프로필 미표시. 도구 게시 계획 승인 전 원격 쓰기 0건"
        rows.append(row)
    return rows
