"""Contract tests pinning the Web harness skill copy to runtime truth.

Issue #306: the skill taught an unconditional `response.status` assertion and
claimed every runner library is declared in both package.json files. The copy
must stay aligned with the TestGenerator HTTP status protocol
(``agents/context/prompts/test_generator.py``) and with the runner
dependencies the web template actually declares per package.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agents.context.prompts.test_generator import get_system_prompt, get_user_prompt


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_PATH = _REPO_ROOT / "skills" / "web-test-harness-skill" / "SKILL.md"
_TEMPLATE_ROOT = _REPO_ROOT / "arc-template" / "templates" / "web-react-express"

# The skill attributes runner libraries per package in sentences of the form
# "`<package>/package.json` provides `lib`, `lib`, and `lib`", so the claims
# can be extracted and checked against the manifest instead of being pinned
# as prose-only review.
_ATTRIBUTION_RE = re.compile(r"`(backend|frontend)/package\.json` provides ([^.;]+)")

# The runner libraries the skill teaches, in claim syntax. A manifest entry
# matching the `@testing-library/*` glob is satisfied by claiming the glob.
_RUNNER_LIBS = ("vitest", "supertest", "@playwright/test", "@testing-library/*")


def _skill() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def _user_prompt() -> str:
    return get_user_prompt(
        node_id="REQ-X",
        requirement_data={"name": "Example", "description": "Example requirement"},
        dynamic_context="",
        interface_contract='{"interface_id":"REQ-X-API-EXAMPLE"}',
    )


def _manifest_runner_deps(package_dir: str) -> set[str]:
    manifest = json.loads(
        (_TEMPLATE_ROOT / package_dir / "package.json").read_text(encoding="utf-8")
    )
    return set(manifest.get("dependencies", {})) | set(manifest.get("devDependencies", {}))


def _glob_covers(manifest_deps: set[str], glob: str) -> bool:
    prefix = glob[:-1]  # drop the trailing `*`
    return any(dep.startswith(prefix) for dep in manifest_deps)


def _claimed_runner_attribution(skill: str) -> dict[str, set[str]]:
    claims: dict[str, set[str]] = {}
    for package, listing in _ATTRIBUTION_RE.findall(skill):
        claims.setdefault(package, set()).update(re.findall(r"`([^`]+)`", listing))
    return claims


def test_skill_runner_attribution_matches_template_manifests() -> None:
    """The per-package claims must stay true against the manifests the web
    template actually ships: every claimed library must exist in that
    package's manifest, and every runner library present in a manifest must
    be claimed for that package — so a dependency change or a dropped claim
    forces a skill-copy update in the same change instead of silent drift."""

    skill = _skill()
    claims = _claimed_runner_attribution(skill)
    assert set(claims) == {"backend", "frontend"}, claims

    manifests = {
        "backend": _manifest_runner_deps("backend"),
        "frontend": _manifest_runner_deps("frontend"),
    }
    for package, claimed in claims.items():
        manifest = manifests[package]
        for token in claimed:
            if token.endswith("/*"):
                assert _glob_covers(manifest, token), (package, token)
            else:
                assert token in manifest, (package, token)
        for runner in _RUNNER_LIBS:
            present = (
                _glob_covers(manifest, runner) if runner.endswith("/*") else runner in manifest
            )
            if not present:
                continue
            covered = any(
                token == runner or (token.endswith("/*") and runner.startswith(token[:-1]))
                for token in claimed
            )
            assert covered, (package, runner, claimed)


def test_skill_does_not_claim_runner_libraries_in_both_packages() -> None:
    """The old bullet claimed `vitest`, `supertest`, `@testing-library/*`,
    and `@playwright/test` are declared in both package.json files; the
    template only ships supertest/@playwright in backend and
    @testing-library in frontend."""

    skill = _skill()

    assert "are already declared in both `package.json` files" not in skill
    # The exclusions the old copy got wrong must stay excluded cross-package.
    claims = _claimed_runner_attribution(skill)
    assert not any(dep.startswith("@testing-library/") for dep in claims.get("backend", set()))
    assert "supertest" not in claims.get("frontend", set())
    assert "@playwright/test" not in claims.get("frontend", set())


def test_skill_status_assertion_requires_a_declared_source() -> None:
    """The API integration recipe must not teach an unconditional status
    assert: exact codes come from the requirement, the interface contract,
    or a verifiable route contract, including non-default 2xx like 201."""

    skill = _skill()

    assert "12. Assert `response.status`, the response envelope" not in skill
    assert "the requirement, the current API interface contract, or a verifiable route contract" in skill
    assert "including 201 or another non-default 2xx" in skill


def test_skill_unknown_status_matches_test_generator_needs_info_protocol() -> None:
    """When no reliable status source exists, the skill must defer to the
    TestGenerator protocol — record `needs-info`, never guess 200 or a 2xx
    range — and the wording must stay aligned with the prompt that owns it."""

    skill = _skill()
    system_prompt = get_system_prompt()
    user_prompt = _user_prompt()

    assert "record `needs-info`" in skill
    assert "do not write `toBe(200)`" in skill
    assert "a broad 2xx matcher" in skill

    assert "record `needs-info`" in system_prompt
    assert "report `needs-info` instead" in user_prompt
