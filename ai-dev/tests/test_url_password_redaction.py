"""Regression tests: URL userinfo passwords containing '/', '%' or '#'.

The old pattern ``[^@/\\s]+`` stopped at '/', leaking the tail of passwords
like ``pa/ss``. Both the code-indexer copy and the swe-agent ConfigMap copy
of inject_context.py are exercised.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

AI_DEV = Path(__file__).resolve().parent.parent
INDEXER_DIR = AI_DEV / "code-indexer"
CONFIGMAP = AI_DEV / "swe-agent" / "configmap.yaml"


def _load_indexer_module():
    spec = importlib.util.spec_from_file_location(
        "inject_context_indexer_copy", INDEXER_DIR / "inject_context.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_configmap_module():
    doc = yaml.safe_load(CONFIGMAP.read_text())
    src = doc["data"]["inject_context.py"]
    mod = types.ModuleType("inject_context_configmap_copy")
    mod.__file__ = str(CONFIGMAP)
    sys.modules[mod.__name__] = mod
    exec(compile(src, f"{CONFIGMAP}:inject_context.py", "exec"), mod.__dict__)
    return mod


@pytest.fixture(scope="module", params=["code-indexer", "configmap"], ids=lambda p: p)
def redact(request):
    loader = (
        _load_indexer_module
        if request.param == "code-indexer"
        else _load_configmap_module
    )
    return loader().redact_secrets


# Passwords are runtime-built fake values; each contains a char the old
# regex mishandled.
CASES = [
    ("slash", "pa" + "/" + "ss"),
    ("percent", "pa" + "%2F" + "ss"),
    ("hash", "pa" + "#" + "ss"),
    ("mixed", "fake" + "/x%" + "40y#z"),
]


@pytest.mark.parametrize("label,password", CASES, ids=[c[0] for c in CASES])
def test_url_password_special_chars_fully_redacted(redact, label, password):
    url = f"https://u:{password}@host/x"
    out = redact(f"clone {url} now")
    assert password not in out
    # No fragment of the password (split on the special char) may survive.
    for piece in ("ss", "y#z", "/x%"):
        if piece in password:
            assert f"{piece}@host" not in out
    assert "https://u:[REDACTED_SECRET]@host/x" in out


def test_configmap_redact_secrets_matches_code_indexer(redact):
    """Both copies must redact identically on a corpus of URL credentials."""
    indexer = _load_indexer_module().redact_secrets
    configmap = _load_configmap_module().redact_secrets
    corpus = "\n".join(f"https://u:{p}@host/x" for _, p in CASES)
    assert indexer(corpus) == configmap(corpus)
