"""Run a project's unit tests and read the result: how many ran, passed, failed, and why each failure failed.

Plain words: the AI never runs the tests. This module does, with one fixed command, then reads the output (JUnit XML
files for Maven and Gradle, the summary lines for pytest, unittest and Jest) and returns a report the screen can show:

    Total 50 tests · Passed 47 · Failed 3

A build that cannot run here at all (no network for dependencies, tool missing) is reported as "could not run", with
the reason, instead of being "debugged".
"""

import os
import re
import shlex
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_SDLC_ARCHITECT_TEST_TIMEOUT", "900"))
TAIL_LINES = 60

# The programs a test command may start. No shell is used, so there are no pipes or chaining either.
RUNNERS = {"mvn", "mvnw", "gradle", "gradlew", "pytest", "py.test", "npm", "yarn", "pnpm", "npx", "go", "cargo",
           "dotnet", "rspec", "bundle", "phpunit", "composer", "sbt", "mix", "swift", "flutter"}
_SHELL_CHARS = re.compile(r"[;&|<>`$\n]")

ENV_FAILURES = re.compile(
    r"Could not resolve dependencies|Could not transfer|Failed to read artifact|could not be resolved|UnknownHostException|"
    r"Connection refused|Network is unreachable|Temporary failure in name resolution|ENOTFOUND|EAI_AGAIN|npm ERR! network|"
    r"command not found|No such file or directory|JAVA_HOME|Unsupported class file|Plugin .* or one of its dependencies", re.I)
JDK_TOO_OLD = re.compile(r"release version \d+ not supported|invalid target release: \S+|Unsupported (?:class file major|major\.minor) version \d+|"
                         r"has been compiled by a more recent version of the Java Runtime", re.I)
BUILD_FAILURES = re.compile(
    r"COMPILATION ERROR|cannot find symbol|compilation failed|error: |SyntaxError|ImportError|ModuleNotFoundError|"
    r"Cannot find module|undefined reference|BUILD FAILURE|FAILURE: Build failed", re.I)


# Environment variables a test command may set in front of it (`JAVA_HOME=/path ./mvnw test`): what a build needs to find its tools.
ALLOWED_ENV = {"JAVA_HOME", "MAVEN_OPTS", "GRADLE_OPTS", "PYTHONPATH", "NODE_OPTIONS", "SPRING_PROFILES_ACTIVE", "GOFLAGS", "RUSTFLAGS", "DOTNET_ROOT"}
_ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def split_env(command: str) -> Tuple[Dict[str, str], str]:
    """('JAVA_HOME=/x ./mvnw test') -> ({'JAVA_HOME': '/x'}, './mvnw test'). Only the allowed variables are taken; anything else is left in
    the command, where validation refuses it."""
    try:
        parts = shlex.split(command or "")
    except ValueError:
        return {}, (command or "").strip()
    env: Dict[str, str] = {}
    while parts and (m := _ASSIGN.match(parts[0])) and m.group(1) in ALLOWED_ENV and not re.search(r"[$`]", m.group(2)):
        env[m.group(1)] = m.group(2)
        parts.pop(0)
    return env, " ".join(shlex.quote(x) if re.search(r"[\s'\"]", x) else x for x in parts)


def validate_command(cmd: str) -> Tuple[bool, str]:
    """Is this an acceptable test command? (A known test runner, no shell tricks; allowed environment variables may come first.)"""
    cmd = (cmd or "").strip()
    if not cmd:
        return False, "No test command is set."
    if _SHELL_CHARS.search(cmd):
        return False, "A test command is a single command: no pipes, chaining or redirects."
    _, cmd = split_env(cmd)
    try:
        parts = shlex.split(cmd)
    except ValueError as exc:
        return False, f"The test command cannot be read: {exc}"
    if not parts:
        return False, "No test command is set."
    exe = Path(parts[0]).name.lower()
    if exe in ("python", "python3"):
        if len(parts) >= 3 and parts[1] == "-m" and parts[2] in ("pytest", "unittest", "nose2"):
            return True, ""
        return False, "Python may only be used as 'python -m pytest' or 'python -m unittest'."
    if exe not in RUNNERS:
        return False, f"'{parts[0]}' is not a known test runner (allowed: {', '.join(sorted(RUNNERS | {'python -m pytest'}))})."
    return True, ""


def detect_command(tree: Path) -> str:
    """The test command a repository's build files point to, for when the manager gave none ('' if unsure)."""
    has = lambda name: (tree / name).exists()
    if has("pom.xml"):
        return "./mvnw test" if has("mvnw") else "mvn test"
    if has("build.gradle") or has("build.gradle.kts"):
        return "./gradlew test" if has("gradlew") else "gradle test"
    if has("package.json"):
        return "yarn test" if has("yarn.lock") else "npm test"
    if has("go.mod"):
        return "go test ./..."
    if has("Cargo.toml"):
        return "cargo test"
    if has("pyproject.toml") or has("pytest.ini") or has("setup.py") or has("requirements.txt"):
        return "python3 -m pytest -q"
    return ""


# ---------- reading results ----------

_JUNIT_GLOBS = ("**/surefire-reports/*.xml", "**/failsafe-reports/*.xml", "**/build/test-results/**/*.xml",
                "**/test-results/**/*.xml")


def clear_old_reports(tree: Path) -> None:
    """Delete report files from earlier runs so only this run's results are read."""
    for pattern in _JUNIT_GLOBS:
        for f in tree.glob(pattern):
            try:
                f.unlink()
            except OSError:
                pass


def _junit(tree: Path, since: Optional[float] = None, until: Optional[float] = None) -> List[Dict[str, str]]:
    cases = []
    seen = set()
    for pattern in _JUNIT_GLOBS:
        for f in tree.glob(pattern):
            if f in seen:
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if (since is not None and mtime < since) or (until is not None and mtime > until):
                continue
            seen.add(f)
            try:
                root = ET.parse(f).getroot()
            except (ET.ParseError, OSError):
                continue
            for tc in root.iter("testcase"):
                name = f"{tc.get('classname', '')}.{tc.get('name', '')}".strip(".")
                bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
                if bad is not None:
                    msg = " ".join(((bad.get("message") or bad.text or "").strip()).split())[:400]
                    cases.append({"name": name, "status": "failed", "message": msg or bad.get("type", "failed")})
                elif tc.find("skipped") is not None:
                    cases.append({"name": name, "status": "skipped", "message": ""})
                else:
                    cases.append({"name": name, "status": "passed", "message": ""})
    return cases


def _count(text: str, word: str) -> int:
    m = re.search(rf"(\d+) {word}", text)
    return int(m.group(1)) if m else 0


def _from_output(output: str) -> Tuple[Optional[Dict[str, int]], List[Dict[str, str]]]:
    """Totals (and failing names) from a runner's summary lines. None when no summary is recognised."""
    failing: List[Dict[str, str]] = []
    # pytest: "=== 3 failed, 47 passed, 1 skipped in 2.1s ==="  /  "FAILED tests/x.py::test_a - AssertionError: ..."
    m = None
    for m in re.finditer(r"^=+ (.*\b(?:passed|failed|error|errors)\b.*?) in [\d.]+s.*=+$", output, re.M):
        pass
    if m:
        s = m.group(1)
        failing = [{"name": n, "status": "failed", "message": msg.strip()[:400]}
                   for n, msg in re.findall(r"^FAILED (\S+)(?: - (.*))?$", output, re.M)]
        failed = _count(s, "failed") + _count(s, "errors?")
        return {"passed": _count(s, "passed"), "failed": failed, "skipped": _count(s, "skipped")}, failing
    # unittest: "Ran 5 tests in 0.002s" ... "FAILED (failures=1, errors=1)"
    m = re.search(r"^Ran (\d+) tests? in", output, re.M)
    if m:
        total = int(m.group(1))
        f = re.search(r"^(?:FAILED|OK)(?: \((.*)\))?$", output, re.M)
        detail = f.group(1) or "" if f else ""
        def num(word: str) -> int:
            n = re.search(rf"{word}=(\d+)", detail)
            return int(n.group(1)) if n else 0
        failed, skipped = num("failures") + num("errors"), num("skipped")
        failing = [{"name": n, "status": "failed", "message": ""} for n in re.findall(r"^(?:FAIL|ERROR): (.+)$", output, re.M)]
        for name, msg in re.findall(r"^(?:FAIL|ERROR): (.+)\n-+\n(?:.*\n)*?(\w*(?:Error|Exception)[^\n]*)", output, re.M):
            for c in failing:
                if c["name"] == name:
                    c["message"] = msg[:400]
        return {"passed": max(total - failed - skipped, 0), "failed": failed, "skipped": skipped}, failing
    # Jest: "Tests:       3 failed, 1 skipped, 47 passed, 51 total"
    m = re.search(r"^Tests:\s+(.*\btotal)$", output, re.M)
    if m:
        s = m.group(1)
        failing = [{"name": n.strip(), "status": "failed", "message": ""} for n in re.findall(r"^\s*● (.+)$", output, re.M)]
        return {"passed": _count(s, "passed"), "failed": _count(s, "failed"), "skipped": _count(s, "skipped")}, failing
    # Maven: "[ERROR] Tests run: 5, Failures: 1, Errors: 0, Skipped: 0" (the last total, not the per-class lines)
    totals = [m for m in re.finditer(r"^\[(?:INFO|ERROR|WARNING)\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$", output, re.M)]
    if totals:
        t, f, e, sk = (int(x) for x in totals[-1].groups())
        failing = [{"name": n, "status": "failed", "message": " ".join(msg.split())[:400]}
                   for n, msg in re.findall(r"^\[ERROR\]\s+(\w[\w.$]*\.\w+):\d+\s+(.*)$", output, re.M)]
        return {"passed": max(t - f - e - sk, 0), "failed": f + e, "skipped": sk}, failing
    # Go (-v): "--- PASS: TestA" / "--- FAIL: TestB"
    passes, fails = re.findall(r"^\s*--- PASS: (\S+)", output, re.M), re.findall(r"^\s*--- FAIL: (\S+)", output, re.M)
    if passes or fails:
        return {"passed": len(passes), "failed": len(fails), "skipped": 0}, [{"name": n, "status": "failed", "message": ""} for n in fails]
    return None, []


def tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def read_report(output: str, exit_code: int, tree: Path, command: str, seconds: float, timed_out: bool = False) -> Dict[str, Any]:
    """Turn a finished run into the report the screen shows."""
    report: Dict[str, Any] = {"ran": True, "command": command, "exit_code": exit_code, "seconds": round(seconds, 1),
                              "total": 0, "passed": 0, "failed": 0, "skipped": 0, "cases": [], "build_failed": False,
                              "note": "", "tail": tail(output)}
    if timed_out:
        return {**report, "ran": False, "note": f"The tests did not finish within {TIMEOUT_SECONDS // 60} minutes, so they were stopped."}
    too_old = JDK_TOO_OLD.search(output)
    if too_old and exit_code != 0:
        return {**report, "ran": False, "note": f"The Java used here is too old for this project ({too_old.group(0)}). Set JAVA_HOME to a newer JDK "
                                                 "in the test command, for example JAVA_HOME=/path/to/jdk ./mvnw test."}
    cases = _junit(tree)
    if cases:
        report["cases"] = cases
        for key, status in (("passed", "passed"), ("failed", "failed"), ("skipped", "skipped")):
            report[key] = sum(1 for c in cases if c["status"] == status)
    else:
        counts, failing = _from_output(output)
        if counts:
            report.update(counts)
            report["cases"] = failing
    report["total"] = report["passed"] + report["failed"] + report["skipped"]
    if report["total"] == 0:
        if exit_code != 0 and ENV_FAILURES.search(output) and not BUILD_FAILURES.search(output.replace("BUILD FAILURE", "")):
            m = ENV_FAILURES.search(output)
            return {**report, "ran": False, "note": f"The build could not run here ({m.group(0)}). Run it where dependencies can be downloaded."}
        if exit_code != 0:
            report["build_failed"] = True
            report["note"] = "The build failed before any test ran."
        else:
            report["note"] = "No tests were found by this command."
    return report


def run(command: str, tree: Path, timeout: Optional[int] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run the test command in the worktree and report. Never raises. `env` (and any variables written in front of the command) are added
    to the environment, but only the allowed ones."""
    ok, why = validate_command(command)
    if not ok:
        return {"ran": False, "command": command, "note": why, "total": 0, "passed": 0, "failed": 0, "skipped": 0, "cases": [],
                "build_failed": False, "exit_code": None, "seconds": 0, "tail": ""}
    inline, bare = split_env(command)
    extra = {k: v for k, v in {**(env or {}), **inline}.items() if k in ALLOWED_ENV}
    parts = shlex.split(bare)
    exe = parts[0]
    if exe.startswith("./") and not (tree / exe[2:]).exists():
        return {"ran": False, "command": command, "note": f"{exe} does not exist in the repository.", "total": 0, "passed": 0,
                "failed": 0, "skipped": 0, "cases": [], "build_failed": False, "exit_code": None, "seconds": 0, "tail": ""}
    clear_old_reports(tree)
    start = time.time()
    try:
        proc = subprocess.run(parts, cwd=tree, capture_output=True, text=True, timeout=timeout or TIMEOUT_SECONDS,
                              env={**os.environ, "CI": "true", "PYTHONDONTWRITEBYTECODE": "1", **extra})      # no stale or stray .pyc files
        output, code, timed_out = (proc.stdout or "") + "\n" + (proc.stderr or ""), proc.returncode, False
    except subprocess.TimeoutExpired as exc:
        output, code, timed_out = ((exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")), -1, True
    except (OSError, ValueError) as exc:
        return {"ran": False, "command": command, "note": f"The command could not be started: {exc}", "total": 0, "passed": 0,
                "failed": 0, "skipped": 0, "cases": [], "build_failed": False, "exit_code": None, "seconds": 0, "tail": ""}
    report = read_report(output, code, tree, bare, time.time() - start, timed_out)
    if extra:
        report["env"] = extra
    return report


def needs_fix(report: Dict[str, Any]) -> bool:
    """Should the coder be asked to fix something? (Tests failed, or the new code does not build.)"""
    return bool(report.get("ran")) and (report.get("failed", 0) > 0 or report.get("build_failed", False))


def headline(report: Dict[str, Any]) -> str:
    if not report.get("ran"):
        return "Tests did not run: " + (report.get("note") or "")
    if report.get("build_failed"):
        return "The build failed before any test ran."
    extra = f" · Skipped {report['skipped']}" if report.get("skipped") else ""
    return f"Total {report['total']} tests · Passed {report['passed']} · Failed {report['failed']}{extra}"


# ---------- the agent's own test runs (it has a shell in the coding step) ----------

_TEST_CMD = re.compile(r"(?:\b(?:mvnw?|gradlew?|npm|yarn|pnpm|npx|cargo|dotnet|sbt|mix|swift|flutter|go|bundle|rspec|phpunit|composer)\b[^\n|;&]*?\b(?:test|verify|check)\b"
                       r"|\bpytest\b|\bpy\.test\b|\bpython3?\s+-m\s+(?:pytest|unittest|nose2)\b)")
_SKIPS = re.compile(r"-DskipTests|-Dmaven\.test\.skip|-x\s+test\b|--no-tests|test-compile|testCompile|-DskipITs")


def is_test_command(command: str) -> bool:
    """Does this shell command run unit tests? (compiling the tests, or skipping them, does not count)"""
    return bool(_TEST_CMD.search(command or "")) and not _SKIPS.search(command or "")


def _build_reason(output: str) -> str:
    """Why a build did not get as far as running tests: the first error lines of its output."""
    m = JDK_TOO_OLD.search(output or "")
    if m:
        return f"The Java used is too old for this project ({m.group(0)}); a newer JDK is needed (JAVA_HOME)."
    lines = [l.strip() for l in (output or "").splitlines() if re.search(r"\[ERROR\]|error:|Error:|FAILED|Exception", l) and l.strip() != "[ERROR]"]
    return " | ".join(" ".join(l.split())[:200] for l in lines[:5])


def read_agent_run(command: str, output: str, is_error: bool, when: str = "", phase: str = "", tree: Optional[Path] = None,
                   ts: Optional[float] = None) -> Dict[str, Any]:
    """What one test run of the agent's own said (a record for the screen; nothing is re-run): its status, every test with its result and the
    reason for each failure. The test reports the run left in the worktree (written within minutes of it) are read first, since quiet builds
    print no summary; otherwise the output text is."""
    cases: List[Dict[str, str]] = []
    source = ""
    counts: Optional[Dict[str, int]] = None
    if tree is not None and ts is not None:
        cases = _junit(tree, since=ts - 900, until=ts + 10)
        if cases:
            counts, source = {k: sum(1 for c in cases if c["status"] == k) for k in ("passed", "failed", "skipped")}, "test report files"
    if counts is None:
        counts, failing = _from_output(output or "")
        cases, source = failing, "console output" if counts else ""
    rec: Dict[str, Any] = {"when": when, "phase": phase, "command": " ".join((command or "").split())[:300], "ok": not is_error,
                           "ran": counts is not None, "passed": 0, "failed": 0, "skipped": 0, "total": 0, "cases": cases[:300], "source": source,
                           "reason": "", "tail": tail(output or "", 40)[-4000:]}
    if counts:
        rec.update(counts)
        rec["total"] = counts["passed"] + counts["failed"] + counts["skipped"]
        rec["status"] = "failed" if rec["failed"] else "passed"
    elif BUILD_FAILURES.search(output or "") or JDK_TOO_OLD.search(output or "") or is_error:
        rec["status"], rec["reason"] = "build_failed", _build_reason(output or "")
    else:
        rec["status"] = "unknown"
    return rec


def run_headline(rec: Dict[str, Any]) -> str:
    if not rec.get("ran"):
        return "the build failed, no test ran" if rec.get("status") == "build_failed" or not rec.get("ok") else "no test results were found"
    return f"{rec['passed']} passed, {rec['failed']} failed" + (f", {rec['skipped']} skipped" if rec["skipped"] else "") + f" of {rec['total']}"


_TEST_FILE = re.compile(r"(^|/)(src/test/|tests?/|__tests__/|spec/)|(^|/)test_[^/]*\.py$|_test\.(py|go|rb)$|(Test|Tests|IT|Spec)\.(java|kt|scala|groovy)$|"
                        r"\.(test|spec)\.[jt]sx?$|Tests?\.(cs|swift)$")


def is_test_file(path: str) -> bool:
    """Is this path a unit-test file (by the usual conventions of each language)?"""
    return bool(_TEST_FILE.search(path or ""))


def latest_run(runs: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The agent's most recent test run that produced results."""
    return next((r for r in reversed(runs or []) if r.get("ran")), None)


def step_result(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """THE test result of a subtask, the one every screen, the pull request and Jira report: what the agent's own last test run saw. If the
    agent ran no tests that produced results, the program's own run stands in. When both exist, the program's re-run is added as a note, so
    a disagreement between them is shown once, in words, instead of as two different numbers."""
    agent, official = latest_run(step.get("agent_runs")), step.get("test_report")
    if agent:
        failing = [{"test": c["name"], "reason": c.get("message") or "it failed; no message was recorded"}
                   for c in agent.get("cases", []) if c.get("status") == "failed"]
        extra = f" · Skipped {agent['skipped']}" if agent.get("skipped") else ""
        rerun = ""
        if official is not None:
            rerun = "The program's own re-run: " + (headline(official) if official.get("ran") and not official.get("build_failed")
                                                    else (official.get("note") or headline(official)))
        return {"source": "agent", "headline": f"Total {agent['total']} tests · Passed {agent['passed']} · Failed {agent['failed']}{extra} (the agent's last run)",
                "ran": True, "failed": agent["failed"], "unresolved": failing, "command": agent["command"], "rerun": rerun}
    if official is None:
        return None
    return {"source": "program", "headline": headline(official), "ran": official.get("ran", False), "failed": official.get("failed", 0),
            "unresolved": official.get("unresolved", []), "command": official.get("command", ""), "rerun": ""}
