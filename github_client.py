"""GitHub CLI transport. Credentials stay inside gh's existing credential store."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass


class SafetyError(RuntimeError):
    pass


@dataclass
class APIError(RuntimeError):
    status: int
    endpoint: str
    uncertain: bool = False

    def __str__(self):
        suffix = "; 결과 불확실: --resume으로 원격 상태를 먼저 확인하세요" if self.uncertain else ""
        return f"GitHub API HTTP {self.status}: {self.endpoint.split('?')[0]}{suffix}"


def redact(value: str) -> str:
    value = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[TOKEN]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
    value = re.sub(r"https://(?:hooks\.|[^/]*webhook)[^\s]+", "[WEBHOOK]", value, flags=re.I)
    return re.sub(r"(?im)^(.*(?:authorization|cookie|token)\s*[:=]).*$", r"\1 [REDACTED]", value)


def command(args, *, input_text=None, timeout=45):
    env = os.environ.copy()
    env.update(GH_PROMPT_DISABLED="1", GH_PAGER="cat", GH_HOST="github.com", GH_DEBUG="")
    return subprocess.run(list(args), input=input_text, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, shell=False, env=env)


def parse_response(output):
    match = re.search(r"\r?\n\r?\n", output)
    if not match:
        return 0, {}, output
    header, body = output[:match.start()], output[match.end():]
    first, *lines = header.splitlines()
    status_match = re.match(r"HTTP/\S+\s+(\d+)", first)
    headers = {key.lower().strip(): value.strip() for line in lines if ":" in line
               for key, value in [line.split(":", 1)]}
    return int(status_match[1]) if status_match else 0, headers, body


class GitHub:
    def __init__(self, *, writable=False, repo="deuxdoom/github-badge", runner=command,
                 sleeper=time.sleep, max_retries=3):
        self.writable = writable
        self.repo = repo
        self.runner = runner
        self.sleep = sleeper
        self.max_retries = max_retries
        self.identity = None
        self.last_headers = {}
        self.last_write = 0.0

    def assert_account(self):
        user = self.get("user")
        if user.get("login") != "deuxdoom":
            raise SafetyError("계정 불일치: deuxdoom 이외의 계정에서는 즉시 종료합니다.")
        self.identity = "deuxdoom"
        return self.identity

    def _allowed_write(self, method, endpoint):
        prefix = f"repos/{self.repo}/"
        if self.repo != "deuxdoom/github-badge" or not endpoint.startswith(prefix):
            return False
        suffix = endpoint[len(prefix):]
        return any(method == verb and re.fullmatch(pattern, suffix) for verb, pattern in [
            ("PUT", r"contents/README\.md"),
            ("POST", r"git/trees"), ("POST", r"git/commits"),
            ("POST", r"git/refs"), ("POST", r"pulls"),
            ("PUT", r"pulls/\d+/merge"),
            ("DELETE", r"git/refs/heads/badge-tool/[a-f0-9]{12}"),
        ])

    def _request(self, method, endpoint, payload=None, *, read_query=False):
        mutation = method != "GET" and not read_query
        if endpoint.startswith(("/", "http")) or ".." in endpoint or "\\" in endpoint:
            raise SafetyError("상대 API endpoint만 허용됩니다.")
        if mutation:
            if not self.writable or not self._allowed_write(method, endpoint):
                raise SafetyError("승인되지 않은 GitHub 쓰기 요청입니다.")
            self.assert_account()  # Recheck immediately before every mutation.
            self.sleep(max(0, 1 - (time.monotonic() - self.last_write)))
        for attempt in range(self.max_retries + 1):
            args = ["gh", "api", "--hostname", "github.com", "--include", "-X", method,
                    endpoint, "-H", "Accept: application/vnd.github+json"]
            if payload is not None:
                args += ["--input", "-"]
            try:
                result = self.runner(args, input_text=json.dumps(payload) if payload is not None else None)
            except (subprocess.TimeoutExpired, OSError) as exc:
                # A timed-out POST may already have succeeded. Never blindly replay it.
                raise APIError(0, endpoint, uncertain=mutation) from exc
            status, headers, body = parse_response(result.stdout)
            self.last_headers = headers
            if mutation:
                self.last_write = time.monotonic()
            if result.returncode == 0 and 200 <= status < 300:
                try:
                    return json.loads(body) if body.strip() else None
                except json.JSONDecodeError as exc:
                    raise APIError(status, endpoint, uncertain=mutation) from exc
            limited = status == 429 or (status == 403 and (
                headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers
                or "rate limit" in body.lower()))
            retryable = limited or status in (500, 502, 503, 504)
            if mutation or not retryable or attempt == self.max_retries:
                raise APIError(status, endpoint, uncertain=mutation and (retryable or status == 0))
            delay = 2 ** attempt
            if limited:
                delay = max(60 * 2 ** attempt, float(headers.get("retry-after", "0")))
                if headers.get("x-ratelimit-remaining") == "0":
                    delay = max(delay, float(headers.get("x-ratelimit-reset", "0")) - time.time() + 1)
            if delay > 60:
                # Persisted checkpoint permits a later resume without blocking for minutes.
                raise APIError(status, endpoint)
            print(f"일시적 API 오류: {delay:.0f}초 후 읽기 요청 재시도 ({attempt + 1}/{self.max_retries})")
            self.sleep(delay)

    def get(self, endpoint):
        return self._request("GET", endpoint)

    def optional(self, endpoint, *, statuses=(404,)):
        try:
            return self.get(endpoint)
        except APIError as exc:
            if exc.status in statuses:
                return None
            raise

    def write(self, method, endpoint, payload=None):
        return self._request(method, endpoint, payload)

    def graphql(self, query):
        if not re.match(r"\s*query\b", query) or re.search(r"\bmutation\b", query):
            raise SafetyError("읽기 전용 GraphQL query만 허용됩니다.")
        result = self._request("POST", "graphql", {"query": query}, read_query=True)
        if result.get("errors"):
            raise SafetyError("GraphQL 조회 실패: 결과를 전체 이력으로 간주할 수 없습니다.")
        return result["data"]

    def pages(self, endpoint, *, max_pages=10):
        separator = "&" if "?" in endpoint else "?"
        items = []
        for page in range(1, max_pages + 1):
            batch = self.get(f"{endpoint}{separator}per_page=100&page={page}")
            items.extend(batch)
            if len(batch) < 100:
                return items, True
        return items, False
