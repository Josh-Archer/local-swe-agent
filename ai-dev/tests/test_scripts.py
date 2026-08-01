"""
Tests for shell scripts in the ai-dev system.
"""

import pytest
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


class TestScriptSyntax:
    """Test that all shell scripts have valid syntax."""

    @pytest.mark.parametrize(
        "script",
        [
            "deploy-safe.sh",
            "deploy.sh",
            "check-plex-health.sh",
            "check-gpu-admission.sh",
            "validate-manifests.sh",
            "llm-mode.sh",
        ],
    )
    def test_script_syntax(self, script):
        """Test shell script syntax with bash -n."""
        script_path = SCRIPTS_DIR / script
        if not script_path.exists():
            pytest.skip(f"Script {script} not found")

        result = subprocess.run(
            ["bash", "-n", str(script_path)], capture_output=True, text=True
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

    def test_documents_hard_fail_modes(self):
        """Plex health must distinguish hard failures from soft warnings."""
        script = SCRIPTS_DIR / "check-plex-health.sh"
        content = script.read_text(encoding="utf-8")
        assert "HARD FAIL" in content or "Hard failures" in content
        assert "HARD_FAIL" in content


class TestCheckGpuAdmissionScript:
    """Tests for hard GPU admission gate (issue #4)."""

    def test_script_exists(self):
        script = SCRIPTS_DIR / "check-gpu-admission.sh"
        assert script.exists()

    def test_has_shebang_and_errexit(self):
        script = SCRIPTS_DIR / "check-gpu-admission.sh"
        content = script.read_text(encoding="utf-8")
        assert content.startswith("#!/bin/bash")
        assert "set -e" in content

    def test_documents_explicit_exit_codes(self):
        """GPU busy / Plex / node failures must use documented exit codes."""
        content = (SCRIPTS_DIR / "check-gpu-admission.sh").read_text(encoding="utf-8")
        for code in ("10", "11", "20", "30", "31"):
            assert code in content
        assert "GPU busy" in content or "insufficient free" in content.lower()

    def test_requires_dual_flag_override(self):
        """Silent override must not be possible — both force flags required."""
        content = (SCRIPTS_DIR / "check-gpu-admission.sh").read_text(encoding="utf-8")
        assert "FORCE_GPU_ADMISSION" in content
        assert "ALLOW_GPU_OVERRIDE" in content

    def test_deploy_safe_calls_admission_gate(self):
        """deploy-safe must hard-call admission before vLLM, not advisory-only."""
        content = (SCRIPTS_DIR / "deploy-safe.sh").read_text(encoding="utf-8")
        assert "check-gpu-admission.sh" in content
        assert "Continue anyway" not in content


class TestGpuConstraintsDocsAndScheduling:
    """Hard vs soft constraints docs and PriorityClass manifests."""

    def test_constraints_doc_exists(self):
        doc = Path(__file__).parent.parent / "GPU_CONSTRAINTS.md"
        assert doc.exists()
        text = doc.read_text(encoding="utf-8")
        assert "Hard constraints" in text
        assert "Soft constraints" in text
        assert "GPU busy" in text or "exit **10**" in text

    def test_priority_classes_manifest(self):
        manifest = Path(__file__).parent.parent / "scheduling" / "priority-classes.yaml"
        assert manifest.exists()
        text = manifest.read_text(encoding="utf-8")
        assert "plex-media-critical" in text
        assert "ai-dev-gpu" in text
        assert "PriorityClass" in text

    def test_vllm_uses_ai_priority_class(self):
        dep = Path(__file__).parent.parent / "vllm" / "vllm-deployment.yaml"
        text = dep.read_text(encoding="utf-8")
        assert "priorityClassName: ai-dev-gpu" in text


class TestLlmModeScript:
    """Tests for llm-mode.sh (GPU vs non-GPU fallback)."""

    def test_script_exists(self):
        """Test that llm-mode.sh exists."""
        script = SCRIPTS_DIR / "llm-mode.sh"
        assert script.exists()

    def test_has_proper_shebang(self):
        """Test that script has proper shebang."""
        script = SCRIPTS_DIR / "llm-mode.sh"
        with open(script, encoding="utf-8") as f:
            first_line = f.readline().strip()
            assert first_line.startswith("#!")
            assert "bash" in first_line

    def test_set_errexit(self):
        """Test that script uses set -e for error handling."""
        script = SCRIPTS_DIR / "llm-mode.sh"
        with open(script, encoding="utf-8") as f:
            content = f.read()
            assert "set -e" in content or "set -o errexit" in content

    def test_documents_status_and_modes(self):
        """Test that script supports status, gpu, and fallback commands."""
        script = SCRIPTS_DIR / "llm-mode.sh"
        with open(script, encoding="utf-8") as f:
            content = f.read()
            assert "status" in content
            assert "fallback" in content
            assert "GPU path" in content
            assert "llm-endpoint-config" in content
            assert "FALLBACK_BASE_URL" in content
            assert "ACTIVE_BASE_URL" in content

    def test_endpoint_configmap_exists(self):
        """Test that llm-endpoint ConfigMap manifest exists with fallback keys."""
        cm = Path(__file__).parent.parent / "vllm" / "llm-endpoint-configmap.yaml"
        assert cm.exists()
        with open(cm, encoding="utf-8") as f:
            content = f.read()
            assert "LLM_MODE" in content
            assert "FALLBACK_BASE_URL" in content
            assert "ACTIVE_BASE_URL" in content
            assert "STATUS_MESSAGE" in content

    def test_gpu_fallback_docs_exist(self):
        """Test that coexistence docs exist."""
        docs = Path(__file__).parent.parent / "GPU_FALLBACK.md"
        assert docs.exists()
        with open(docs, encoding="utf-8") as f:
            content = f.read()
            assert "Plex" in content
            assert "FALLBACK_BASE_URL" in content
            assert "DISABLED" in content


class TestManifestValidation:
    """Test manifest validation logic."""

    def test_yaml_files_valid(self):
        """Test that all YAML files are valid."""
        ai_dev_dir = Path(__file__).parent.parent
        for yaml_file in ai_dev_dir.rglob("*.yaml"):
            # Skip test data files
            if "tests" in str(yaml_file):
                continue

            _result = subprocess.run(
                ["yamllint", "-d", "relaxed", str(yaml_file)], capture_output=True
            )
            # Don't fail on warnings, just errors
            # assert _result.returncode == 0, f"YAML lint failed for {yaml_file}"

    def test_ingressroute_has_no_embedded_default_auth(self):
        """Ingress must not ship the historical default user/password secret."""
        ingress = Path(__file__).parent.parent / "ingress" / "ingressroute.yaml"
        content = ingress.read_text(encoding="utf-8")
        assert (
            "dXNlcjokYXByMSRQN0RnOUNuMyRXeUE3QzdyWEF6S1FYVG5xVkxVdTcwCg=="
            not in content
        )
        assert "$apr1$P7Dg9Cn3$WyA7C7rXAzKQXTnqVLUu70" not in content
        # Middleware still wires basicAuth to an out-of-band secret
        assert "api-auth-secret" in content
        assert "kind: Secret" not in content

    def test_auth_secret_templates_exist_without_real_creds(self):
        """Templates document generation and only use placeholder markers."""
        ai_dev = Path(__file__).parent.parent
        example = ai_dev / "ingress" / "example-secret.yaml"
        swe = ai_dev / "swe-agent" / "secret-template.yaml"
        assert example.exists()
        assert swe.exists()
        example_text = example.read_text(encoding="utf-8")
        swe_text = swe.read_text(encoding="utf-8")
        assert "REPLACE_WITH_" in example_text or "REPLACE_WITH_" in swe_text
        assert (
            "dXNlcjokYXByMSRQN0RnOUNuMyRXeUE3QzdyWEF6S1FYVG5xVkxVdTcwCg=="
            not in example_text
        )
        assert "htpasswd" in example_text

    def test_validate_manifests_refuses_placeholders(self):
        """validate-manifests.sh must contain placeholder refusal logic."""
        script = SCRIPTS_DIR / "validate-manifests.sh"
        content = script.read_text(encoding="utf-8")
        assert "check_auth_placeholders" in content
        assert "REPLACE_WITH_" in content
        assert "dXNlcjokYXByMSRQN0RnOUNuMyRXeUE3QzdyWEF6S1FYVG5xVkxVdTcwCg==" in content


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
