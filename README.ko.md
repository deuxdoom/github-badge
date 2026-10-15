# GitHub 업적 감사 도구

Python 3.13 이상과 인증된 GitHub CLI가 필요합니다. 별도 Python 패키지는 설치하지 않습니다. `gh api --hostname github.com user --jq .login`이 `deuxdoom`인지 확인하며 다른 계정이면 중단합니다.

```powershell
Set-Location -LiteralPath 'F:\temp\GitHubGetBadgeAutomation'
python run.py --audit
python run.py --dry-run
python -m unittest discover -v
python run.py --execute
```

`--execute`는 감사 → 동결된 계획과 파일 해시 미리보기 → 로컬 테스트 → 승인 → 실제 구현 PR 1건 게시 → 병합 확인 → 업적 재조회 → 생성 브랜치 정리를 순서대로 수행합니다. 대화형 터미널에서 표시된 `APPROVE 해시앞12자리`를 입력해야 합니다. 비대화형 실행에서는 검토한 계획의 전체 SHA256을 `--approve` 인수로 명시해야 합니다. 코드를 수정하면 `--dry-run`으로 다시 계획을 만들고 승인해야 합니다.

Quickdraw와 Starstruck은 `EARNED / USER_CONFIRMED / SKIPPED`로 고정합니다. 공개 프로필에 표시된 다른 업적도 건너뜁니다. 미표시는 숨김 여부를 알 수 없어 `UNKNOWN`입니다. 로그인한 프로필에서 이미 보유한 업적을 추가로 확인했다면 `config.example.json`을 `config.json`으로 복사해 `user_confirmed_earned`에 추가하고 `--dry-run --config config.json`을 사용하세요. 계정과 게시 대상은 변경할 수 없습니다.

승인 대상은 현재 비어 있는 `deuxdoom/github-badge`에 최초 README 커밋과 도구 소스 PR 1건을 게시하는 작업입니다. 기존 파일이 생겼거나 기본 브랜치/가시성이 바뀌면 중단합니다. 다른 저장소와 기존 프로젝트는 수정하지 않습니다. 의미 없는 PR 반복, 거짓 공동 작성, 자동 스타/팔로우, 가짜 답변 채택은 구현하지 않았습니다.

리뷰 의무를 우회하지 않습니다. 보호된 main, 활성 규칙, 추가된 리뷰, 실패/진행 중 검사, 변경된 SHA가 있으면 병합을 중단합니다. API가 병합 상태를 아직 계산 중이면 나중에 `--resume`으로 재개하세요. GitHub API의 병합과 프로필 업적 표시를 각각 확인하므로 업적 획득은 보장하지 않습니다.

```powershell
python run.py --resume
python run.py --cleanup
python run.py --report
```

승인과 실행 진행은 `.state/checkpoint.json`에 원자적으로 저장하며 동시 실행을 잠급니다. 재개 시 원격 PR/브랜치/병합 결과를 먼저 조회합니다. 응답 유실로 결과가 불확실하고 원격에서도 확인되지 않으면 같은 쓰기를 자동 반복하지 않습니다. 읽기 요청은 429/5xx에서 제한된 지수 백오프를 적용하고, `Retry-After`와 rate-limit reset을 존중합니다. 대기 시간이 60초를 초과하면 긴 대기 대신 중단하고 나중에 재개하도록 합니다.

Pair Extraordinaire는 실제 공동 작업자의 기여·동의·계정 이메일 연결이 필요합니다. 이 도구가 수행하지 않는 공동 작성 흐름은 `REAL_COAUTHOR_REQUIRED`로 보고합니다. Galaxy Brain은 실제 질문 조사 자료만 수집하며 근거와 검증을 갖춘 답변 초안의 사용자 승인과 자발적인 채택이 필요합니다. 답변 자동 게시·자기 채택 기능은 없습니다. Public Sponsor의 공개 후원과 결제는 사용자가 직접 수행해야 합니다. 이 조건이 없으면 `BLOCKED`이며 획득으로 표시하지 않습니다.

`RESULT.ko.md`와 `.state/report.json`에서 상태·증빙·생성 자원을 확인할 수 있습니다. `.state/audit.json`은 실행 전 읽기 전용 감사, `.state/audit-after.json`은 실행 후 감사, `.state/preview.json`은 검토용 계획입니다. 업적 등급 수치는 공식 보증치로 하드코딩하지 않습니다.

정리 범위는 이번 실행에서 생성했고 병합이 확인된 정확한 작업 브랜치 하나입니다. 다른 사용자의 추가 변경이 있으면 삭제하지 않습니다. 원격 저장소, main, PR/커밋 기록, 소스와 보고서는 보존합니다. 서버 감사 기록이나 외부 캐시의 완전 삭제는 불가능하며 브랜치 삭제로 PR 기록이 사라지지 않습니다. 실패한 작업 브랜치는 증빙과 복구를 위해 남겨 둡니다. 기존 `gh auth`를 로그아웃하거나 토큰 권한을 바꾸지 않습니다.

공식 참고: [프로필 업적](https://docs.github.com/en/account-and-profile/reference/profile-reference#earning-achievements), [API 권장 방식](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api), [허용되는 사용 정책](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies).
