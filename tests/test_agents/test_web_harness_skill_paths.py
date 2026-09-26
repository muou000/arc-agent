"""Regression pins for Web harness examples under the staged test domain."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.runtime.capabilities import is_node_test_path, stable_node_path_segment
from agents.runtime.import_checks import classify_import
from agents.tools.test_manifest import TestManifestLock, build_declare_test_manifest_tool


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_PATH = _REPO_ROOT / "skills" / "web-test-harness-skill" / "SKILL.md"
_NODE_ID = "REQ-1"
_SEGMENT = stable_node_path_segment(_NODE_ID)

# These are the same directory shapes taught by the skill. Keeping the
# importer directory and target beside each example makes the path-depth
# contract executable instead of relying on a prose-only review.
_WEB_SAMPLES = (
    (
        "backend/tests/generated/<stable-segment>/domainRepository.test.js",
        "backend/tests/generated/<stable-segment>",
        "../../../src/repositories/domainRepository.js",
        "backend/src/repositories/domainRepository.js",
    ),
    (
        "backend/tests/generated/<stable-segment>/integration/domainApi.test.js",
        "backend/tests/generated/<stable-segment>/integration",
        "../../../../src/app.js",
        "backend/src/app.js",
    ),
    (
        "frontend/tests/generated/<stable-segment>/DomainPage.test.tsx",
        "frontend/tests/generated/<stable-segment>",
        "../../../src/api/domain.js",
        "frontend/src/api/domain.js",
    ),
    (
        "backend/test-e2e/generated/<stable-segment>/login.e2e.spec.js",
        "backend/test-e2e/generated/<stable-segment>",
        None,
        None,
    ),
)


def test_skill_examples_use_the_current_node_namespace_and_resolve_imports() -> None:
    skill = _SKILL_PATH.read_text(encoding="utf-8")

    assert "backend/tests/<domain>Repository.test.js" not in skill
    assert "from '../src/" not in skill
    assert "import('../src/app.js')" not in skill
    assert "generated/<stable-segment>" in skill
    assert "Compute every relative import from the test file's actual directory" in skill

    for path_template, importer_template, specifier, target in _WEB_SAMPLES:
        assert path_template in skill
        path = path_template.replace("<stable-segment>", _SEGMENT)
        importer_dir = importer_template.replace("<stable-segment>", _SEGMENT)
        assert is_node_test_path(path, _NODE_ID), path
        if specifier is None:
            continue
        assert specifier in skill
        assert (
            classify_import(specifier, importer_dir, lambda candidate: candidate == target) is None
        ), (path, specifier, target)


def test_legacy_web_test_root_still_works_without_namespace_enforcement() -> None:
    """The staged namespace is an opt-in boundary, not a global path migration."""

    legacy_path = "backend/tests/domainRepository.test.js"
    assert not is_node_test_path(legacy_path, _NODE_ID)
    tool = build_declare_test_manifest_tool(
        node_id=_NODE_ID,
        manifest_lock=TestManifestLock(node_id=_NODE_ID, enforce_node_namespace=False),
    )
    payload = json.loads(
        asyncio.run(tool(files=[{"file_path": legacy_path, "type": "Unit"}]))
    )
    assert payload["status"] == "locked"
