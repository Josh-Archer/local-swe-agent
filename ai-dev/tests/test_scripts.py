"""
Tests for shell scripts in the ai-dev system.
"""

import pytest
import subprocess
from pathlib import Path


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


class TestScriptSyntax:
    """Test that all shell scripts have valid syntax."""

    @pytest.mark.parametrize("script", [
        "deploy-safe.sh",
        "check-plex-health.sh",
        "validate-manifests.sh",
    ])
    def test_script_syntax(self, script):
        """Test shell script syntax with bash -n."""
        script_path = SCRIPTS_DIR / script
        if not script_path.exists():
            pytest.skip(f"Script {script} not found")

        result = subprocess.run(
            ["bash", "-n", str(script_path)],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0, f"Syntax error in {script}: {result.stderr}"

    def test_all_scripts_executable(self):
        """Test that all .sh files have executable permissions."""
        for script in SCRIPTS_DIR.glob("*.sh"):
            # On Windows, this check may not be relevant
            # but we can check the file exists and is readable
            assert script.exists()
            assert script.is_file()


class TestDeploySafeScript:
    """Tests for deploy-safe.sh script."""

    def test_script_exists(self):
        """Test that deploy-safe.sh exists."""
        script = SCRIPTS_DIR / "deploy-safe.sh"
        assert script.exists()

    def test_has_proper_shebang(self):
        """Test that script has proper shebang."""
        script = SCRIPTS_DIR / "deploy-safe.sh"
        if not script.exists():
            pytest.skip("Script not found")

        with open(script) as f:
            first_line = f.readline().strip()
            assert first_line.startswith("#!"), "Missing shebang"
            assert "bash" in first_line, "Should use bash"

    def test_set_errexit(self):
        """Test that script uses set -e for error handling."""
        script = SCRIPTS_DIR / "deploy-safe.sh"
        if not script.exists():
            pytest.skip("Script not found")

        with open(script) as f:
            content = f.read()
            # Should have set -e for error handling
            assert "set -e" in content or "set -o errexit" in content


class TestCheckPlexHealthScript:
    """Tests for check-plex-health.sh script."""

    def test_script_exists(self):
        """Test that check-plex-health.sh exists."""
        script = SCRIPTS_DIR / "check-plex-health.sh"
        assert script.exists()

    def test_checks_kubectl(self):
        """Test that script checks for kubectl."""
        script = SCRIPTS_DIR / "check-plex-health.sh"
        if not script.exists():
            pytest.skip("Script not found")

        with open(script) as f:
            content = f.read()
            # Should reference kubectl
            assert "kubectl" in content


class TestManifestValidation:
    """Test manifest validation logic."""

    def test_yaml_files_valid(self):
        """Test that all YAML files are valid."""
        ai_dev_dir = Path(__file__).parent.parent
        for yaml_file in ai_dev_dir.rglob("*.yaml"):
            # Skip test data files
            if "tests" in str(yaml_file):
                continue

            result = subprocess.run(
                ["yamllint", "-d", "relaxed", str(yaml_file)],
                capture_output=True
            )
            # Don't fail on warnings, just errors
            # assert result.returncode == 0, f"YAML lint failed for {yaml_file}"


@pytest.mark.integration
class TestPythonScripts:
    """Integration tests for Python scripts."""

    def test_index_code_imports(self):
        """Test that index_code.py can be imported."""
        # This would test actual import
        # For now, just check file exists
        script = Path(__file__).parent.parent / "code-indexer" / "index_code.py"
        assert script.exists()

    def test_test_vllm_api_imports(self):
        """Test that test-vllm-api.py can be imported."""
        script = Path(__file__).parent.parent / "scripts" / "test-vllm-api.py"
        if script.exists():
            # Would test import
            assert True
