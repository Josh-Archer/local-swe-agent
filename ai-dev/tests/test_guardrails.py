"""
Tests for guarded issue-to-PR demo workflow artifacts and guardrails-check.sh.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

AI_DEV = Path(__file__).parent.parent
SCRIPTS = AI_DEV / "scripts"
SWE = AI_DEV / "swe-agent"
GUARDRAILS = SCRIPTS / "guardrails-check.sh"
LAUNCHER = SCRIPTS / "run-guarded-issue-job.sh"
JOB_TEMPLATE = SWE / "job-template.yaml"
CONFIGMAP = SWE / "configmap.yaml"
DOCS = SWE / "GUARDED_ISSUE_TO_PR.md"


def _bash_available():
    try:
        subprocess.run(
            ["bash", "--version"], capture_output=True, check=True, text=True
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _bash_path(path: Path) -> str:
    """Return a path string usable by the local bash (WSL or Git Bash)."""
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)

    posix = resolved.as_posix()  # C:/Users/...
    if len(posix) < 2 or posix[1] != ":":
        return posix

    drive = posix[0].lower()
    rest = posix[2:]  # /Users/...

    # Prefer WSL mount if present (Windows ships System32\bash.exe -> WSL)
    wsl = f"/mnt/{drive}{rest}"
    git_bash = f"/{drive}{rest}"
    try:
        probe = subprocess.run(
            ["bash", "-c", f'test -e "{wsl}" && echo wsl || (test -e "{git_bash}" && echo git || echo none)'],
            capture_output=True,
            text=True,
            check=False,
        )
        kind = (probe.stdout or "").strip()
        if kind == "wsl":
            return wsl
        if kind == "git":
            return git_bash
    except OSError:
        pass
    return wsl


BASH = _bash_available()
requires_bash = pytest.mark.skipif(not BASH, reason="bash not available")

class TestGuardedArtifacts:
    """Static checks that demo workflow files exist and declare acceptance criteria."""

    def test_job_template_exists(self):
        assert JOB_TEMPLATE.is_file()

    def test_job_template_accepts_issue_number_and_label(self):
        text = JOB_TEMPLATE.read_text(encoding="utf-8")
        assert "ISSUE_NUMBER" in text
        assert "ISSUE_LABEL" in text
        assert "GITHUB_OWNER" in text
        assert "GITHUB_REPO" in text
        assert "PATH_ALLOWLIST" in text
        assert "MAX_CHANGED_FILES" in text
        assert "ALLOW_FORCE_PUSH" in text
        assert 'value: "false"' in text  # OPEN_PR / force-push defaults

    def test_job_template_is_valid_yaml(self):
        docs = list(yaml.safe_load_all(JOB_TEMPLATE.read_text(encoding="utf-8")))
        assert len(docs) >= 1
        job = docs[0]
        assert job["kind"] == "Job"
        env = job["spec"]["template"]["spec"]["containers"][0]["env"]
        names = {e["name"] for e in env}
        for required in (
            "ISSUE_NUMBER",
            "ISSUE_LABEL",
            "PATH_ALLOWLIST",
            "MAX_CHANGED_FILES",
            "ALLOW_FORCE_PUSH",
            "OPEN_PR",
            "REQUIRE_HUMAN_APPROVAL",
        ):
            assert required in names

    def test_configmap_has_guardrails_and_runner(self):
        text = CONFIGMAP.read_text(encoding="utf-8")
        assert "guardrails.yaml" in text
        assert "guardrails-check.sh" in text
        assert "run-swe-agent.sh" in text
        assert "REQUIRE_HUMAN_APPROVAL" in text
        assert "PATH_ALLOWLIST" in text
        assert "ALLOW_FORCE_PUSH" in text

    def test_docs_cover_human_approval_and_walkthrough(self):
        assert DOCS.is_file()
        text = DOCS.read_text(encoding="utf-8")
        assert "Human approval" in text or "human approval" in text
        assert "walkthrough" in text.lower() or "Example walkthrough" in text
        assert "PATH_ALLOWLIST" in text or "allowlist" in text.lower()
        assert "force-push" in text.lower() or "force push" in text.lower()
        assert "max" in text.lower()

    def test_launcher_and_checker_exist(self):
        assert GUARDRAILS.is_file()
        assert LAUNCHER.is_file()


@requires_bash
class TestGuardrailsCheckScript:
    """Behavioral tests for guardrails-check.sh."""

    def _run(self, repo: Path, env=None, base="HEAD"):
        """Run guardrails-check; pass options as flags (WSL drops some env vars)."""
        env = env or {}
        cmd = [
            "bash",
            _bash_path(GUARDRAILS),
            "--repo-dir",
            _bash_path(repo),
            "--base",
            base,
        ]
        if "PATH_ALLOWLIST" in env:
            cmd.extend(["--allowlist", env["PATH_ALLOWLIST"]])
        if "MAX_CHANGED_FILES" in env:
            cmd.extend(["--max-files", str(env["MAX_CHANGED_FILES"])])
        if "ALLOW_FORCE_PUSH" in env:
            cmd.extend(["--allow-force-push", env["ALLOW_FORCE_PUSH"]])
        if "ENFORCE_GUARDRAILS" in env:
            cmd.extend(["--enforce", env["ENFORCE_GUARDRAILS"]])

        # Force-push markers must be visible inside the script; inject via bash -c
        force_exports = []
        for key in ("FORCE_PUSH", "GIT_PUSH_FORCE", "GIT_PUSH_OPTS"):
            if key in env:
                force_exports.append(f'export {key}={subprocess.list2cmdline([env[key]])}')

        if force_exports:
            inner = " && ".join(force_exports + [subprocess.list2cmdline(cmd)])
            run_cmd = ["bash", "-c", inner]
        else:
            run_cmd = cmd

        return subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            check=False,
        )

    def _git(self, repo: Path, *args):
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    @pytest.fixture
    def git_repo(self, temp_dir):
        repo = temp_dir / "repo"
        repo.mkdir()
        self._git(repo, "init")
        self._git(repo, "config", "user.email", "test@example.com")
        self._git(repo, "config", "user.name", "Test")
        (repo / "README.md").write_text("root\n", encoding="utf-8")
        (repo / "ai-dev").mkdir()
        (repo / "ai-dev" / "ok.txt").write_text("ok\n", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-m", "init")
        # Ensure master/main exists as HEAD for diffs
        return repo

    def test_script_syntax(self):
        result = subprocess.run(
            ["bash", "-n", _bash_path(GUARDRAILS)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_launcher_syntax(self):
        result = subprocess.run(
            ["bash", "-n", _bash_path(LAUNCHER)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_passes_allowlisted_small_change(self, git_repo):
        (git_repo / "ai-dev" / "ok.txt").write_text("changed\n", encoding="utf-8")
        self._git(git_repo, "add", "ai-dev/ok.txt")
        self._git(git_repo, "commit", "-m", "allowlisted")
        # base is parent
        base = subprocess.check_output(
            ["git", "rev-parse", "HEAD~1"], cwd=git_repo, text=True
        ).strip()
        result = self._run(
            git_repo,
            env={
                "PATH_ALLOWLIST": "ai-dev/,*.md",
                "MAX_CHANGED_FILES": "10",
                "ALLOW_FORCE_PUSH": "false",
            },
            base=base,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "GUARDRAILS PASSED" in result.stdout

    def test_fails_non_allowlisted_path(self, git_repo):
        secret = git_repo / "secrets"
        secret.mkdir()
        (secret / "token").write_text("x\n", encoding="utf-8")
        self._git(git_repo, "add", "secrets/token")
        self._git(git_repo, "commit", "-m", "bad path")
        base = subprocess.check_output(
            ["git", "rev-parse", "HEAD~1"], cwd=git_repo, text=True
        ).strip()
        result = self._run(
            git_repo,
            env={
                "PATH_ALLOWLIST": "ai-dev/",
                "MAX_CHANGED_FILES": "10",
                "ENFORCE_GUARDRAILS": "true",
            },
            base=base,
        )
        assert result.returncode == 1
        assert "not allowlisted" in result.stdout

    def test_fails_max_files(self, git_repo):
        for i in range(3):
            p = git_repo / "ai-dev" / f"f{i}.txt"
            p.write_text(f"{i}\n", encoding="utf-8")
        self._git(git_repo, "add", "ai-dev")
        self._git(git_repo, "commit", "-m", "many files")
        base = subprocess.check_output(
            ["git", "rev-parse", "HEAD~1"], cwd=git_repo, text=True
        ).strip()
        result = self._run(
            git_repo,
            env={
                "PATH_ALLOWLIST": "ai-dev/",
                "MAX_CHANGED_FILES": "2",
                "ENFORCE_GUARDRAILS": "true",
            },
            base=base,
        )
        assert result.returncode == 1
        assert "MAX_CHANGED_FILES" in result.stdout

    def test_blocks_force_push_env(self, git_repo):
        result = self._run(
            git_repo,
            env={
                "PATH_ALLOWLIST": "ai-dev/,*.md",
                "MAX_CHANGED_FILES": "10",
                "ALLOW_FORCE_PUSH": "false",
                "FORCE_PUSH": "true",
                "ENFORCE_GUARDRAILS": "true",
            },
            base="HEAD",
        )
        assert result.returncode == 1
        assert "force-push" in result.stdout.lower() or "force" in result.stdout.lower()

    def test_launcher_dry_run_renders_issue_number(self):
        result = subprocess.run(
            [
                "bash",
                _bash_path(LAUNCHER),
                "--repo",
                "acme/demo",
                "--issue",
                "42",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "acme" in result.stdout
        assert "demo" in result.stdout
        assert "42" in result.stdout
        assert "swe-agent-issue-42" in result.stdout
        # OPEN_PR stays false without --approved
        assert "name: OPEN_PR" in result.stdout
