"""The web template's registration contract must match its on-disk glue.

`registration_contract_lines` is what the agents are told; `design_denied_write_paths`
is what the DESIGN-stage discipline mechanically blocks. Both must describe the
same reality: every denied path exists in the checked-in template, and the
registration modules the contract names exist too. A drift between the two
turns either into agents being blocked with no alternative or into merge
conflicts the denylist was built to prevent.
"""

from __future__ import annotations

from pathlib import Path

from app_type_handler import AppTypeHandler, WebAppType, get_app_type_handler_class

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = REPO_ROOT / "arc-template" / "templates" / "web-react-express"


class TestRegistrationContractLines:
    def test_web_contract_names_every_registration_mechanism(self) -> None:
        text = "\n".join(WebAppType.registration_contract_lines())
        for marker in (
            "backend/src/routes/",
            ".routes.js",
            "mountPath",
            "backend/src/database/schema/",
            ".schema.js",
            "apply(db)",
            "frontend/src/pages/",
            "export const route",
            "frontend/src/sections/home/",
            "sectionOrder",
            "frontend/src/providers/",
        ):
            assert marker in text, f"registration contract is missing: {marker}"

    def test_web_contract_forbids_every_denied_glue_file(self) -> None:
        contract = "\n".join(WebAppType.registration_contract_lines())
        for path in WebAppType.design_denied_write_paths():
            assert path in contract, f"denied glue path is not covered by the contract: {path}"

    def test_other_app_types_have_no_registration_contract(self) -> None:
        for app_type in ("android", "cli"):
            handler = get_app_type_handler_class(app_type)
            assert handler.registration_contract_lines() == []
            assert handler.design_denied_write_paths() == []

    def test_base_class_defaults_are_empty(self) -> None:
        assert AppTypeHandler.registration_contract_lines() == []
        assert AppTypeHandler.design_denied_write_paths() == []


class TestDesignDeniedWritePaths:
    def test_every_denied_path_exists_in_template(self) -> None:
        for relative in WebAppType.design_denied_write_paths():
            assert (TEMPLATE_ROOT / relative).is_file(), (
                f"denied glue path does not exist in the template: {relative}"
            )

    def test_denied_paths_stay_minimal_and_glue_only(self) -> None:
        # The denylist is a tripwire around shared composition files, not a
        # general write restriction: everything outside it stays node-private.
        paths = WebAppType.design_denied_write_paths()
        assert len(paths) <= 20
        assert all(path.startswith(("frontend/src/", "backend/src/")) for path in paths)

    def test_contract_modules_exist_in_template(self) -> None:
        for relative in (
            "backend/src/routes",
            "backend/src/database/schema",
            "frontend/src/sections/home",
            "frontend/src/providers",
            "frontend/src/api/index.ts",
        ):
            assert (TEMPLATE_ROOT / relative).exists(), (
                f"registration contract names a missing template location: {relative}"
            )
