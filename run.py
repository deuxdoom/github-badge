"""Single entry point: audit -> plan -> dry run -> approval -> publish -> verify -> cleanup."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from achievements import Publisher, make_plan, preview, statuses, verify_local_bundle
from audit import collect, now
from cleanup import cleanup
from github_client import APIError, GitHub, PendingAction, SafetyError, redact
from storage import ROOT, STATE, digest, load, locked, save


def report(audit, state):
    rows = statuses(audit, state)
    original = load(STATE / "initial-audit.json", audit)
    for row in rows:
        row["initial"] = original["achievements"][row["achievement"]]["status"]
        if (state.get("merged") and row["achievement"] in ("YOLO", "Pull Shark")
                and row["initial"] not in ("VERIFIED", "SKIPPED")):
            row["action"] = "감사 도구 구현 PR 1건 병합"
    data = {"generated_at": now(), "audit_observed_at": audit["observed_at"], "rows": rows,
        "source": str(ROOT), "remote_resources": {
            "repo": "deuxdoom/github-badge (기존 저장소; 삭제 대상 아님)",
            "bootstrap_commit": state.get("base_sha"), "source_commit": state.get("commit_sha"),
            "pr_url": state.get("pr_url"), "created_branch": state.get("branch"),
            "branch_cleaned": state.get("branch_cleaned", False)},
        "local_resources": [str(ROOT), str(STATE), str(ROOT / "evidence")],
        "cleanup": "로컬 증빙·소스 보존; 원격 정리는 승인된 생성 브랜치만",
        "records": "PR·커밋·이벤트·GitHub 서버 감사 기록과 외부 캐시는 완전 삭제할 수 없습니다.",
        "resume": f'Set-Location -LiteralPath "{ROOT}"; python run.py --resume',
        "publication_state": state.get("status", "NOT_EXECUTED"), "error": state.get("error")}
    save(STATE / "report.json", data)
    lines = ["# GitHub 업적 작업 결과", "", f"감사 시각(UTC): {audit['observed_at']}", "",
        "공개 프로필 미표시는 미획득의 확정 증거가 아닙니다. BLOCKED는 필요한 활동 조건을 뜻하며 보유 여부를 단정하지 않습니다.", "",
        "업적 | 최초 상태 | 수행 작업 | 최종 상태 | 등급 | 근거/URL | 후속 조건",
        "--- | --- | --- | --- | --- | --- | ---"]
    def safe(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    for row in rows:
        initial = "EARNED / USER_CONFIRMED" if row["achievement"] in ("Quickdraw", "Starstruck") else row["initial"]
        lines.append(" | ".join(safe(value) for value in (row["achievement"], initial, row["action"],
                     row["status"], row["tier"], row["evidence"], row["next"])))
    lines += ["", f"소스: `{ROOT}`", "", f"원격 실행 상태: {data['publication_state']}", "",
              f"생성/정리 자원: `{json.dumps(data['remote_resources'], ensure_ascii=False)}`", "",
              "이번 실행의 로컬 소스, .state 체크포인트/감사/보고서, evidence 자료는 보존합니다. 원격 저장소를 삭제하지 않습니다.", "",
              data["records"], "", "안전한 재개:", "", "```powershell", data["resume"], "```", ""]
    if data["error"]:
        lines += ["실행 중단 이유: " + data["error"], ""]
    (ROOT / "RESULT.ko.md").write_text(redact("\n".join(lines)), encoding="utf-8")
    return data


def print_report(data):
    for row in data["rows"]:
        print(f"{row['achievement']}: {row['status']} ({row['tier']}) — {row['next']}")
    print(f"보고서: {ROOT / 'RESULT.ko.md'}")


def tests_pass():
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-v"],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=120, shell=False)
    if result.returncode:
        print(redact(result.stdout + result.stderr))
        raise SafetyError("로컬 테스트가 실패하여 원격 쓰기를 중단했습니다.")
    print("로컬 모의 테스트 통과")


def main(argv=None, client_factory=GitHub):
    parser = argparse.ArgumentParser(description="deuxdoom 전용 GitHub 업적 감사 및 승인 기반 소스 게시")
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("audit", "dry-run", "execute", "resume", "cleanup", "report"):
        modes.add_argument("--" + mode, action="store_true")
    parser.add_argument("--config", help="옵션 JSON; 실제 계정/대상 변경은 허용하지 않음")
    parser.add_argument("--approve", metavar="SHA256", help="이미 검토한 계획의 전체 SHA256으로 명시 승인")
    args = parser.parse_args(argv)
    if args.approve and not (args.execute or args.resume):
        parser.error("--approve는 --execute/--resume과 함께 사용하세요.")
    if sys.version_info < (3, 13):
        raise SafetyError("Python 3.13 이상이 필요합니다.")
    with locked():
        audit = load(STATE / "audit.json")
        state = load(STATE / "checkpoint.json", {})
        plan = load(STATE / "plan.json")
        if args.report:
            if not audit:
                raise SafetyError("감사 결과가 없습니다. --audit부터 실행하세요.")
            print_report(report(audit, state))
            return 0
        client = client_factory()
        client.assert_account()
        if args.audit or args.dry_run:
            audit = collect(client)
            save(STATE / "audit.json", audit)
            if args.dry_run:
                if state.get("approved_digest"):
                    raise SafetyError("승인된 실행의 계획을 덮어쓰지 않습니다. --resume 또는 --report를 사용하세요.")
                plan = make_plan(audit, load(args.config, {}) if args.config else {})
                save(STATE / "plan.json", plan)
                save(STATE / "preview.json", preview(plan))
                print(json.dumps(preview(plan), ensure_ascii=False, indent=2))
            print_report(report(audit, state))
            return 0
        if args.cleanup:
            if not plan or not state.get("approved_digest"):
                raise SafetyError("정리할 승인된 실행이 없습니다.")
            client.writable = True
            cleanup(Publisher(client, plan, state))
            print_report(report(audit, state))
            return 0
        if args.resume and not plan:
            raise SafetyError("재개할 계획이 없습니다. --dry-run부터 실행하세요.")
        current = collect(client)
        if not plan:
            plan = make_plan(current, load(args.config, {}) if args.config else {})
            save(STATE / "plan.json", plan)
        verify_local_bundle(plan)
        if not audit:
            audit = current
        for name in plan.get("user_confirmed_earned", []):
            current["achievements"][name].update(status="SKIPPED", current_status="EARNED",
                                                 verification="USER_CONFIRMED", next_action="SKIP")
        save(STATE / "audit.json", current)
        save(STATE / "preview.json", preview(plan))
        print(json.dumps(preview(plan), ensure_ascii=False, indent=2))
        if plan["blocked"]:
            print_report(report(current, state))
            raise SafetyError("; ".join(plan["blocked"]))
        if not state.get("approved_digest") and all(current["achievements"][name]["status"] == "VERIFIED"
                                                     for name in ("YOLO", "Pull Shark")):
            print_report(report(current, state))
            print("두 업적 모두 이미 표시되어 추가 활동을 생략합니다.")
            return 0
        tests_pass()
        if current["rate_limit"]["core"]["remaining"] < 100:
            raise SafetyError("남은 core API 할당량이 100 미만입니다. reset 이후 재개하세요.")
        approved_digest = digest(plan)
        if not state.get("approved_digest"):
            if args.approve:
                if args.approve != approved_digest:
                    raise SafetyError("승인 해시가 현재 계획과 다릅니다.")
            elif sys.stdin.isatty():
                expected = "APPROVE " + approved_digest[:12]
                answer = input(f"위 원격 생성·병합·브랜치 정리에 동의하면 {expected} 입력: ")
                if answer != expected:
                    raise SafetyError("승인이 없어 원격 쓰기를 수행하지 않았습니다.")
            else:
                print_report(report(current, state))
                raise SafetyError("비대화형 실행: 검토 후 --approve 전체_SHA256를 명시해야 합니다.")
            verify_local_bundle(plan)
            state = {"run_id": plan["run_id"], "branch": plan["branch"], "approved_digest": approved_digest,
                     "approved_at": now(), "status": "APPROVED", "pending": None}
            save(STATE / "checkpoint.json", state)
            save(STATE / "initial-audit.json", current)
        elif state["approved_digest"] != approved_digest:
            raise SafetyError("체크포인트와 계획의 승인 해시가 다릅니다.")
        client.writable = True
        publisher = Publisher(client, plan, state)
        try:
            publisher.execute()
            verified = collect(client)
            for name in plan.get("user_confirmed_earned", []):
                verified["achievements"][name].update(status="SKIPPED", current_status="EARNED",
                                                      verification="USER_CONFIRMED", next_action="SKIP")
            save(STATE / "audit-after.json", verified)
            save(STATE / "audit.json", verified)
            cleanup(publisher)
            state.update(status="COMPLETED", error=None)
            publisher.save()
            final = report(verified, state)
            print_report(final)
            return 0
        except (APIError, SafetyError, subprocess.SubprocessError, OSError) as exc:
            state.update(status="PENDING" if isinstance(exc, PendingAction) or (
                         isinstance(exc, APIError) and exc.uncertain) else "BLOCKED",
                         error=redact(str(exc)))
            publisher.save()
            print_report(report(current, state))
            raise


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (SafetyError, APIError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(redact(str(error)), file=sys.stderr)
        raise SystemExit(2)
