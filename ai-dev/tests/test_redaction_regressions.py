"""Issue #28/#29 QA cases; fake credential values are assembled at runtime."""

import ast
import builtins
import gc
import inspect
import json
from pathlib import Path
import time

import pytest
import yaml

from test_url_password_redaction import (  # exercise both deployed copies
    _load_configmap_module,
    _load_indexer_module,
)


@pytest.fixture(
    params=[_load_indexer_module, _load_configmap_module], ids=["source", "configmap"]
)
def injector(request):
    return request.param()


@pytest.fixture
def redact(injector):
    return injector.redact_secrets


@pytest.fixture
def value():
    return "".join(["runtime", "fixture", "731"])


# Numbered IDs map directly to the QA findings, including the later follow-ups.
LEAK_CASES = [
    ("B2-admin", "adminPassword: VALUE"),
    ("B2-root", "auth.rootPassword: VALUE"),
    ("B2-db", 'const dbPassword = "VALUE"'),
    ("B2-json-camel", '{"adminPassword": "VALUE"}'),
    ("B2-passwd", "DB_PASSWD=VALUE"),
    ("B2-pwd", "MYSQL_PWD=VALUE"),
    ("B2-repeated-separator", "DB__PASSWD=VALUE"),
    ("legacy-keys-suffix", "SSH_KEYS=VALUE"),
    ("legacy-app-key", "APP_KEY=VALUE"),
    ("B2-dotted", '{"spring.datasource.password": "VALUE"}'),
    ("B3-anchor", "password: &pw VALUE\ncopy: *pw\n"),
    ("B3-quoted-anchor", 'password: &pw "VALUE"\ncopy: *pw\n'),
    ("B3-anchor-flow", "base: &x {password: VALUE}\ncopy: *x\n"),
    ("B4-env", '{"env":[{"name":"MYSQL_ROOT_PASSWORD","value":"VALUE"}]}'),
    ("B4-env-reversed", '{"env":[{"value":"VALUE","name":"MYSQL_ROOT_PASSWORD"}]}'),
    ("B5a-argv-yaml", 'command: ["sshpass", "-p", "VALUE"]'),
    ("B5a-argv-json", '{"command":["sshpass", "-p", "VALUE"]}'),
    ("B5a-shell", "sshpass -p VALUE ssh host"),
    ("B5a-shell-quoted", 'sshpass -p "VALUE" ssh host'),
    ("N2-go", 'password := "VALUE"'),
    ("N4-python-typed", 'password: str = "VALUE"'),
    ("N4-ts-typed", 'let token: string = "VALUE"'),
    ("nested-yaml-mapping", "password:\n  value: VALUE\n"),
    ("nested-yaml-key", "api_key:\n  key: VALUE\n"),
    ("nested-yaml-sequence", "token:\n  - VALUE\n"),
    ("nested-yaml-flow", "password: {value: VALUE}"),
    ("nested-json-mapping", '{"password":{"value":"VALUE"},"keep":true}'),
    ("nested-json-sequence", '{"token":["VALUE"]}'),
    ("multi-document", "keep: true\n---\nenv:\n- name: DB_PASSWORD\n  value: VALUE\n"),
    ("fenced-yaml", "```yaml\nenv:\n- name: DB_PASSWORD\n  value: VALUE\n```"),
    ("k8s-yaml", "env:\n- name: DB_PASSWORD\n  value: VALUE\n"),
    ("k8s-malformed", "broken: [\nenv:\n- name: DB_PASSWORD\n  value: VALUE\n"),
    ("dotted-env-value", "DB_PASSWORD=VALUE.suffix"),
    ("quoted-compose", '- "MYSQL_ROOT_PASSWORD=VALUE"'),
    ("folded-yaml", "token: >-\n  VALUE\nother: keep\n"),
    ("literal-yaml", "password: |\n  VALUE\n  more-private\nother: keep\n"),
    ("BOM-key", "\ufeffpass\ufeffword: VALUE\n"),
    ("BOM-k8s-tail", "env:\n- name: DB_PASSWORD\n  value: VALUE\n\ufeff"),
    (
        "BOM-k8s-comment",
        "env:\n- name: DB_PASSWORD\n  value: VALUE\n\ufeff# next\nfoo: bar\n",
    ),
    ("inline-dict", 'creds = {"password": "VALUE", "user": "bob"}'),
    ("partial-json", '  "password": "VALUE",\n "host": "db"'),
    ("call-expression-literal", 'login(password="VALUE")'),
    (
        "go-tag-trailing-secret",
        'Password string `json:"password"` // DB_PASSWORD=VALUE',
    ),
    ("shell-docker", "docker login -u bob -p VALUE registry.invalid"),
    ("shell-mysql", "command: mysql -uroot -pVALUE"),
    (
        "shell-curl",
        "curl --user " + ":".join(["bob", "VALUE"]) + " https://example.invalid",
    ),
]


@pytest.mark.parametrize("case,template", LEAK_CASES, ids=[c[0] for c in LEAK_CASES])
def test_secret_leaks(redact, value, case, template):
    output = redact(template.replace("VALUE", value))
    assert value not in output
    assert "[REDACTED_SECRET]" in output


BENIGN_CASES = [
    ("B1-code", 'parser.add_argument("--level", default="info")'),
    ("B1-yaml", "default: info"),
    ("B1-number", "d.get(k, default=0)"),
    ("B5b-argparse", 'add_argument("-p", "--port")'),
    ("B5b-print", "find . -print"),
    ("B5b-prune", "find . -prune"),
    ("B5b-pthread", "gcc -pthread"),
    ("B5b-pv", "mkdir -pv out"),
    ("B5b-cp", "cp -pr src dest"),
    ("B5b-mysql-port", "mysql -P3306 --host localhost"),
    ("N3-call", "authorization = get_auth_header()"),
    ("N3-fastapi", "authorization: str = Header(None)"),
    ("N4-existingSecret", "existingSecret: my-db-secret"),
    ("N4-pageToken", "pageToken: nextPageToken"),
    ("N4-primary_key", "primary_key: id"),
    ("N4-routing-key", "routing-key: orders"),
    ("N4-cache_key", 'cache_key = f"user:{uid}"'),
    ("N4-ts-type", "token: number;"),
    ("N4-pydantic-type", "password: SecretStr"),
    ("N4-password-prose", "password is required"),
    ("N4-bearer-prose", "Bearer authentication"),
    ("expression-call", "password = get_password()"),
    ("expression-reference", "password = self.password"),
    ("expression-env", 'password = os.environ["DB_PASSWORD"]'),
    ("template-reference", 'PGPASSWORD="${PGPASSWORD}"'),
    ("tag-reference", "password: !ENV ${DB_PASSWORD}"),
    ("go-json-tag", 'Password string `json:"password"`'),
    ("schema", "password:\n  required: true"),
    ("benign-k8s-env", '{"env":[{"name":"PORT","value":"8080"}]}'),
]


@pytest.mark.parametrize("case,source", BENIGN_CASES, ids=[c[0] for c in BENIGN_CASES])
def test_false_positives(redact, case, source):
    assert redact(source) == source


@pytest.mark.parametrize(
    "scalar", ["0", "12345", "1.25", "true", "false", "null", "1e5"]
)
def test_N1_json_non_string_types(redact, scalar):
    source = '{"password": ' + scalar + ', "keep": [1, true, null]}'
    assert redact(source) == source


@pytest.mark.parametrize(
    "scalar",
    ["~", "null", "true", "FALSE", "yes", "off", "12345", "1.25", "1:20:30", ".5"],
)
def test_N1_yaml_non_string_types(redact, scalar):
    source = "password: " + scalar + "\n"
    assert redact(source) == source


@pytest.mark.parametrize(
    "source",
    [
        '{\n\t"password" : "VALUE",\n\t"keep": [1,  2]\n}\n',
        '{ "password" : "VALUE", "escaped": "\\u0061", "count":1.0 }\r\n',
        '{\n"password": "VALUE",\n"other": {}\n}',
    ],
)
def test_N5_json_exact_layout(redact, value, source):
    original = source.replace("VALUE", value)
    output = redact(original)
    assert output == source.replace("VALUE", "[REDACTED_SECRET]")
    json.loads(output)


@pytest.mark.parametrize(
    "template",
    [
        'password: &pw "VALUE"\ncopy: *pw\nkeep: yes\n',
        "password: &pw VALUE\ncopy: *pw\nkeep: yes\n",
        "password: &pw >-\n  VALUE\ncopy: *pw\nkeep: yes\n",
        "token: >-\n  VALUE\nother: keep\n",
        "password: |\n  VALUE\n  more-private\nother: keep\n",
        'command: ["sshpass", "-p", "VALUE"]',
        "password:\n  value: VALUE\n",
        "token: &loop [VALUE, *loop]\n",
    ],
)
def test_yaml_structure(redact, value, template):
    source = template.replace("VALUE", value)
    output = redact(source)
    result = yaml.safe_load(output)
    assert value not in output
    assert result
    if "copy:" in source:
        assert result["password"] == result["copy"] == "[REDACTED_SECRET]"
    if "other:" in source:
        assert result["other"] == "keep"
        assert "more-private" not in output
    assert redact(output) == output


@pytest.mark.parametrize(
    "template",
    [
        'password: \ufeff"VALUE"\nkeep: yes\n',
        "password: \ufeffVALUE\nkeep: yes\n",
        'password: "VAL\ufeffUE"\nkeep: yes\n',
        '\ufeffpassword: &pw \ufeff"VALUE"\ncopy: *pw\n',
        '{"password": \ufeff"VALUE", "keep": 1}',
    ],
)
def test_BOM_before_and_inside_values(redact, value, template):
    source = template.replace("VALUE", value).replace(
        "VAL\ufeffUE", value[:3] + "\ufeff" + value[3:]
    )
    output = redact(source)
    assert value not in output.replace("\ufeff", "")
    assert output.count("\ufeff") == source.count("\ufeff")
    clean = output.replace("\ufeff", "")
    parsed = json.loads(clean) if clean.startswith("{") else yaml.safe_load(clean)
    assert parsed["password"] == "[REDACTED_SECRET]"
    assert redact(output) == output


@pytest.mark.parametrize("number", [str(73519000 + 46), "0"])
def test_k8s_numeric_values(redact, number):
    source = "env:\n- name: DB_PASSWORD\n  value: " + number + "\n"
    output = redact(source)
    assert number not in output
    assert yaml.safe_load(output)["env"][0]["value"] == "[REDACTED_SECRET]"


def without_yaml(monkeypatch):
    original_import = builtins.__import__

    def import_without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("optional dependency disabled")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_yaml)


@pytest.mark.parametrize(
    "template",
    [
        "password: VALUE",
        "adminPassword: VALUE",
        "password: &pw VALUE\ncopy: *pw\n",
        'command: ["sshpass", "-p", "VALUE"]',
        "password: >-\n  VALUE\nother: keep\n",
        "env:\n- name: DB_PASSWORD\n  value: VALUE\n",
        '{"env":[{"name":"DB_PASSWORD","value":"VALUE"}]}',
        "sshpass -p VALUE ssh host",
    ],
)
def test_N7_optional_yaml(redact, monkeypatch, value, template):
    without_yaml(monkeypatch)
    assert value not in redact(template.replace("VALUE", value))


@pytest.mark.parametrize(
    "scalar,is_string",
    [
        ("1e5", True),
        ("1.0e5", True),
        ("tRue", True),
        ("True", False),
        ("1.0e+5", False),
        ("1:20:30", False),
        ("~", False),
        (".5", False),
    ],
)
def test_fallback_matches_yaml_11_scalars(redact, monkeypatch, scalar, is_string):
    assert isinstance(yaml.safe_load("v: " + scalar)["v"], str) == is_string
    without_yaml(monkeypatch)
    source = "password: " + scalar
    output = redact(source)
    assert (output != source) == is_string


def test_N6_no_stale_unused_regex(injector):
    # The QA's dead quoted_value assignment and stale JSON-island claim must
    # stay gone. Inspect both modules, including the deployed source copy.
    source = (
        inspect.getsource(injector)
        if injector.__name__ != "inject_context_configmap_copy"
        else yaml.safe_load(Path(injector.__file__).read_text())["data"][
            "inject_context.py"
        ]
    )
    tree = ast.parse(source)
    assert not any(
        isinstance(n, ast.Name) and n.id == "quoted_value" for n in ast.walk(tree)
    )
    assert "JSON-island" not in source
    assert "optional PyYAML" in ast.get_docstring(tree)


def test_deep_json_without_recursion(redact, value):
    source = "[" * 5000 + '{"password":"' + value + '"}' + "]" * 5000
    output = redact(source)
    assert value not in output
    assert output == source.replace(value, "[REDACTED_SECRET]")


@pytest.mark.parametrize(
    "kind",
    [
        "json",
        "yaml",
        "code",
        "many-boms",
        "pem-begins",
        "pem-complete",
        "certificate-tails",
    ],
)
def test_linear_performance(redact, value, kind):
    def sample(count):
        if kind == "json":
            return (
                '{"items":['
                + ",".join('{"password":"' + value + '"}' for _ in range(count))
                + "]}"
            )
        if kind == "yaml":
            return "items:\n" + ("  - password: " + value + "\n") * count
        if kind == "code":
            return "password = get_password()\n" * (count * 5)
        if kind == "many-boms":
            return (
                "items:\n"
                + ('\ufeff  - pass\ufeffword: \ufeff"' + value + '"\n') * count
            )
        dashes = "-" * 5
        private_key = " ".join(["PRIVATE", "KEY"])
        begin = f"{dashes}BEGIN {private_key}{dashes}\n"
        end = f"{dashes}END {private_key}{dashes}\n"
        if kind == "pem-begins":
            return begin * (count * 3)
        if kind == "pem-complete":
            return (begin + value + "\n" + end) * count
        return "YWJjMTIzNDU2\n" + "-----END CERTIFICATE-----\n" * count

    timings = []
    for count in (5000, 20000):
        source = sample(count)
        runs = []
        for _ in range(3):
            # Collect unrelated test objects before timing and take the best
            # of repeated runs to reduce scheduler/GC noise on shared runners.
            gc.collect()
            started = time.perf_counter()
            output = redact(source)
            runs.append(time.perf_counter() - started)
        timings.append(min(runs))
        if kind == "code":
            assert output == source
        else:
            assert value not in output
    # Independent absolute and relative bounds: the ratio must be able to fail
    # below the absolute cap (unlike the QA's max(1.0, t0) * 8 assertion).
    assert timings[1] < 7.5
    assert timings[1] / max(timings[0], 0.001) < 8


def test_embedded_injector_is_identical():
    module = _load_indexer_module()
    deployed = _load_configmap_module()
    assert (
        yaml.safe_load(Path(deployed.__file__).read_text())["data"]["inject_context.py"]
        == Path(module.__file__).read_text()
    )


@pytest.mark.parametrize(
    "template",
    [
        "command: mysql -uroot -pVALUE",
        "sshpass -p 'VALUE' ssh host",
        'sshpass -p "VALUE" ssh host',
    ],
)
def test_shell_commands_preserve_surrounding_syntax(redact, value, template):
    assert redact(template.replace("VALUE", value)) == template.replace(
        "VALUE", "[REDACTED_SECRET]"
    )


def test_go_tag_placeholder_collision(redact):
    source = 'Password string `json:"password"`\n__CODE_INDEXER_GO_TAG_0__ __CODE_INDEXER_GO_TAG_99__'
    assert redact(source) == source
