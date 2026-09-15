"""Directed, marker-guarded fixes for the platform-provisioned app templates.

A run compiles against the templates the platform provisions through
``ARC_AGENT_TEMPLATES_ROOT`` (see ``app_type_handler/base.py``); the in-repo
``arc-template/templates`` tree only mirrors them. A fix that must reach every
generated workspace therefore cannot live as an edit to the template source -
the provisioned template would never carry it, every workspace would be
generated from the unfixed files, and every node would pay for the defect in
its own TDD loop. Fixes are delivered here instead and applied to the workspace
immediately after the template is copied.

Every edit is *directed* and *marker-guarded*:

- directed: it replaces one known pre-fix code shape, never a whole file;
- already fixed: the fix marker is present, so the edit is a no-op;
- unknown shape: neither the pre-fix nor the fixed shape is recognizable, so
  the file is reported and left untouched instead of being clobbered - the
  caller fails the scaffold, because a template that evolved upstream must not
  be silently overwritten nor silently compiled against;
- absent targets: the template ships none of a patch's target files, so there
  is nothing to patch and the patch is skipped.

A patch may declare prerequisite patches (``requires``); a prerequisite that
was not applied or already present makes the dependent patch unrecognized,
because its search shapes assume the prerequisite's output. Registration order
is validated so a dependent can never be registered before its prerequisite.

A patch is applied atomically: if any of its edits cannot be classified, none
of them is written, and the writes that do happen are staged and swapped
together, so a workspace can never end up half-patched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateEdit:
    """One directed text replacement inside a copied template file."""

    relative_path: str
    search: str
    replace: str
    applied_marker: str


@dataclass(frozen=True)
class TemplatePatch:
    """A named fix, delivered as edits to one or more template files."""

    name: str
    template_id: str
    summary: str
    edits: tuple[TemplateEdit, ...]
    # Patch names that must be applied (or already present) before this one.
    # A dependent's search shapes assume its prerequisites' output, so a
    # missing prerequisite makes it unclassifiable rather than silently skipped.
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class PatchOutcome:
    """What happened to one patch on one workspace."""

    patch_name: str
    status: str  # "applied" | "already-applied" | "unrecognized" | "skipped"
    detail: str


APPLIED = "applied"
ALREADY_APPLIED = "already-applied"
UNRECOGNIZED = "unrecognized"
SKIPPED = "skipped"


_INIT_DB_PATH = "backend/src/database/init_db.js"
_README_PATH = "README.md"

# PR #28 fixed two defects in the template's database bootstrap. Both are
# delivered here instead of as repo template edits, because the template that
# actually compiles the workspace is provisioned by the platform:
#
#  1. initializeDatabase() could hand a caller a closed handle when a
#     concurrent closeDb()/setDbPath() invalidated an in-flight first init, so
#     the caller's next DB operation failed with
#     "SQLITE_MISUSE: Database is closed", and the interrupted init could
#     surface as an unhandled rejection.
#  2. A genuine init failure was swallowed into the bounded retry loop instead
#     of surfacing, so a real error burned every attempt before being reported.
TEMPLATE_PATCHES: tuple[TemplatePatch, ...] = (
    TemplatePatch(
        name="init-db-never-return-closed-handle",
        template_id="web-react-express",
        summary=(
            "initializeDatabase() resolves to an open handle for the current "
            "generation, and closeDb() absorbs an orphaned init rejection"
        ),
        edits=(
            TemplateEdit(
                relative_path=_INIT_DB_PATH,
                search=(
                    "const DEFAULT_DB_FILENAME = 'database.db';\n"
                    "\n"
                    "let db = null;\n"
                ),
                replace=(
                    "const DEFAULT_DB_FILENAME = 'database.db';\n"
                    "\n"
                    "// Upper bound for initializeDatabase() attempts when a concurrent closeDb() /\n"
                    "// setDbPath() invalidates an in-flight initialization. Genuine init errors\n"
                    "// still surface (rethrown) once attempts are exhausted.\n"
                    "const MAX_INIT_ATTEMPTS = 5;\n"
                    "\n"
                    "let db = null;\n"
                ),
                applied_marker="const MAX_INIT_ATTEMPTS = 5;",
            ),
            TemplateEdit(
                relative_path=_INIT_DB_PATH,
                search=(
                    "async function initializeDatabase(options = {}) {\n"
                    "  if (options.dbPath) {\n"
                    "    await setDbPath(options.dbPath);\n"
                    "  }\n"
                    "  if (options.reset) {\n"
                    "    await resetDatabaseFile();\n"
                    "  }\n"
                    "  if (initPromise) {\n"
                    "    // Memoized path: callers await this function and then use the result as a\n"
                    "    // database handle. Returning the init promise would hand them a\n"
                    "    // Promise<void>, so every second DB operation failed with\n"
                    "    // \"Cannot read properties of undefined (reading 'exec')\".\n"
                    "    await initPromise;\n"
                    "    return getDb();\n"
                    "  }\n"
                    "\n"
                    "  const database = getDb();\n"
                    "  initPromise = (async () => {\n"
                ),
                replace=(
                    "function startInit() {\n"
                    "  const database = getDb();\n"
                    "  const promise = (async () => {\n"
                ),
                applied_marker="function startInit() {",
            ),
            TemplateEdit(
                relative_path=_INIT_DB_PATH,
                search=(
                    "  })();\n"
                    "\n"
                    "  try {\n"
                    "    await initPromise;\n"
                    "  } catch (error) {\n"
                    "    initPromise = null;\n"
                    "    throw error;\n"
                    "  }\n"
                    "\n"
                    "  return database;\n"
                    "}\n"
                    "\n"
                    "function closeDb() {\n"
                    "  if (!db) {\n"
                    "    initPromise = null;\n"
                    "    return Promise.resolve();\n"
                    "  }\n"
                    "\n"
                    "  const currentDb = db;\n"
                    "  db = null;\n"
                    "  initPromise = null;\n"
                ),
                replace=(
                    "\n"
                    "    return database;\n"
                    "  })();\n"
                    "  // If a concurrent closeDb() invalidates the handle mid-init, absorb the\n"
                    "  // rejection so it cannot become an unhandled rejection; initializeDatabase()\n"
                    "  // callers still observe it through their own await and retry against the\n"
                    "  // current generation.\n"
                    "  promise.catch(() => {});\n"
                    "  initPromise = promise;\n"
                    "  return promise;\n"
                    "}\n"
                    "\n"
                    "async function initializeDatabase(options = {}) {\n"
                    "  if (options.dbPath) {\n"
                    "    await setDbPath(options.dbPath);\n"
                    "  }\n"
                    "  if (options.reset) {\n"
                    "    await resetDatabaseFile();\n"
                    "  }\n"
                    "\n"
                    "  // Invariant: every resolved return value is an open handle for the current\n"
                    "  // generation. A concurrent closeDb()/setDbPath() can invalidate an in-flight\n"
                    "  // init; re-validate before handing the handle out and retry against the\n"
                    "  // current state instead of silently returning a closed database (which made\n"
                    "  // the next DB operation fail with \"SQLITE_MISUSE: Database is closed\").\n"
                    "  let lastError = null;\n"
                    "  for (let attempt = 0; attempt < MAX_INIT_ATTEMPTS; attempt += 1) {\n"
                    "    const pending = initPromise;\n"
                    "    if (pending) {\n"
                    "      try {\n"
                    "        const database = await pending;\n"
                    "        if (db === database) {\n"
                    "          return database;\n"
                    "        }\n"
                    "        // Generation was swapped while waiting; fall through and re-check.\n"
                    "      } catch (error) {\n"
                    "        lastError = error;\n"
                    "        if (initPromise === pending) {\n"
                    "          initPromise = null;\n"
                    "        }\n"
                    "      }\n"
                    "      continue;\n"
                    "    }\n"
                    "\n"
                    "    try {\n"
                    "      const database = await startInit();\n"
                    "      if (db === database) {\n"
                    "        return database;\n"
                    "      }\n"
                    "    } catch (error) {\n"
                    "      lastError = error;\n"
                    "    }\n"
                    "  }\n"
                    "\n"
                    "  throw lastError || new Error('Database initialization did not produce a usable handle');\n"
                    "}\n"
                    "\n"
                    "function closeDb() {\n"
                    "  const pendingInit = initPromise;\n"
                    "  initPromise = null;\n"
                    "  if (pendingInit) {\n"
                    "    // The in-flight init may reject with SQLITE_MISUSE once its handle is\n"
                    "    // closed underneath it. Absorb that rejection here so it never surfaces\n"
                    "    // as an unhandled rejection; initializeDatabase() awaiters observe the\n"
                    "    // same error through their own await and retry against the current state.\n"
                    "    pendingInit.catch(() => {});\n"
                    "  }\n"
                    "  if (!db) {\n"
                    "    return Promise.resolve();\n"
                    "  }\n"
                    "\n"
                    "  const currentDb = db;\n"
                    "  db = null;\n"
                ),
                applied_marker="pendingInit.catch(() => {});",
            ),
            TemplateEdit(
                relative_path=_README_PATH,
                search=(
                    "- The default database file is `database.db`, unless `ARC_DB_FILE` or `DATABASE_FILE` is set.\n"
                ),
                replace=(
                    "- The default database file is `database.db`, unless `ARC_DB_FILE` or `DATABASE_FILE` is set.\n"
                    "- `initializeDatabase()` always resolves to an open handle for the current database path, even when `closeDb()`/`setDbPath()` race an in-flight initialization; it never returns a closed handle.\n"
                ),
                applied_marker="always resolves to an open handle for the current database path",
            ),
        ),
    ),
    TemplatePatch(
        name="init-db-rethrow-genuine-init-failures",
        template_id="web-react-express",
        summary=(
            "a rejection observed while its init promise is still current is a "
            "genuine failure and surfaces instead of burning the retries"
        ),
        edits=(
            TemplateEdit(
                relative_path=_INIT_DB_PATH,
                search=(
                    "      } catch (error) {\n"
                    "        lastError = error;\n"
                    "        if (initPromise === pending) {\n"
                    "          initPromise = null;\n"
                    "        }\n"
                    "      }\n"
                    "      continue;\n"
                    "    }\n"
                    "\n"
                    "    try {\n"
                    "      const database = await startInit();\n"
                    "      if (db === database) {\n"
                    "        return database;\n"
                    "      }\n"
                    "    } catch (error) {\n"
                    "      lastError = error;\n"
                    "    }\n"
                ),
                replace=(
                    "      } catch (error) {\n"
                    "        if (initPromise === pending) {\n"
                    "          initPromise = null;\n"
                    "          throw error;\n"
                    "        }\n"
                    "        lastError = error;\n"
                    "      }\n"
                    "      continue;\n"
                    "    }\n"
                    "\n"
                    "    const promise = startInit();\n"
                    "    try {\n"
                    "      const database = await promise;\n"
                    "      if (db === database) {\n"
                    "        return database;\n"
                    "      }\n"
                    "    } catch (error) {\n"
                    "      if (initPromise === promise) {\n"
                    "        initPromise = null;\n"
                    "        throw error;\n"
                    "      }\n"
                    "      lastError = error;\n"
                    "    }\n"
                ),
                applied_marker="if (initPromise === promise) {",
            ),
            TemplateEdit(
                relative_path=_INIT_DB_PATH,
                search=(
                    "  // the next DB operation fail with \"SQLITE_MISUSE: Database is closed\").\n"
                    "  let lastError = null;\n"
                ),
                replace=(
                    "  // the next DB operation fail with \"SQLITE_MISUSE: Database is closed\").\n"
                    "  //\n"
                    "  // Distinguishing invalidation from failure: every operation that changes\n"
                    "  // `db` (closeDb, setDbPath, a newer startInit) also nulls or replaces\n"
                    "  // `initPromise`. So when the promise we awaited is still the current\n"
                    "  // initPromise, nothing invalidated our generation and the rejection is a\n"
                    "  // genuine init failure \u2014 surface it immediately instead of burning retries.\n"
                    "  let lastError = null;\n"
                ),
                applied_marker="Distinguishing invalidation from failure",
            ),
        ),
        requires=("init-db-never-return-closed-handle",),
    ),
)


def patches_for(template_id: str) -> tuple[TemplatePatch, ...]:
    """Patches registered for one template, in application order.

    Registration order is part of the contract: a dependent patch's search
    shapes assume its prerequisites' output, so a dependency registered after
    its dependent is a broken registration and raises instead of silently
    skipping the dependent.
    """

    selected = tuple(patch for patch in TEMPLATE_PATCHES if patch.template_id == template_id)
    positions = {patch.name: position for position, patch in enumerate(selected)}
    for patch in selected:
        for prerequisite in patch.requires:
            prerequisite_position = positions.get(prerequisite)
            if prerequisite_position is None:
                raise ValueError(
                    f"Template patch {patch.name!r} requires {prerequisite!r}, "
                    "which is not registered for the same template"
                )
            if prerequisite_position > positions[patch.name]:
                raise ValueError(
                    f"Template patch {prerequisite!r} must be registered before its "
                    f"dependent {patch.name!r}"
                )
    return selected


def _read_text(path: str) -> str:
    # newline="" keeps the file's own line endings: the search/replace strings
    # are written for LF and are re-encoded for a CRLF file, so a patch never
    # rewrites the untouched lines of a file it edits.
    with open(path, "r", encoding="utf-8", newline="") as file:
        return file.read()


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.write(text)


def _classify(text: str, edit: TemplateEdit) -> tuple[str, str]:
    """Report how one edit relates to a file's current content.

    The fix markers are single lines, so line endings never affect matching
    them. A file that carries the marker and no longer contains the pre-fix
    shape is done. For the edits that replace the pre-fix shape, a file carrying
    both is a half-repaired state this module never produces, and guessing which
    of the two is current is how a broken bootstrap would get reported as fixed
    - it is surfaced instead. Edits that append after the searched text keep it
    visible in the fixed file, so coexistence is their normal post-patch state.
    """

    marker_present = edit.applied_marker in text
    newline = "\r\n" if "\r\n" in text else "\n"
    search = edit.search if newline == "\n" else edit.search.replace("\n", "\r\n")
    occurrences = text.count(search)
    keeps_search_visible = edit.search in edit.replace
    if marker_present and (occurrences == 0 or keeps_search_visible):
        return ALREADY_APPLIED, f"{edit.relative_path} already carries the fix"
    if occurrences == 0:
        return UNRECOGNIZED, (
            f"{edit.relative_path} contains neither the known pre-fix shape nor the fix marker"
        )
    if occurrences > 1:
        return UNRECOGNIZED, (
            f"{edit.relative_path} contains the known pre-fix shape {occurrences} times"
        )
    if marker_present:
        return UNRECOGNIZED, (
            f"{edit.relative_path} carries the fix marker next to the pre-fix shape"
        )
    return APPLIED, edit.relative_path


def _patched_text(text: str, edit: TemplateEdit) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    search = edit.search if newline == "\n" else edit.search.replace("\n", "\r\n")
    replace = edit.replace if newline == "\n" else edit.replace.replace("\n", "\r\n")
    return text.replace(search, replace, 1)


def _remove(paths) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def _restore(paths: list[str], originals: dict[str, str]) -> str:
    """Best-effort rollback of targets a failed swap already replaced."""

    failed: list[str] = []
    for path in paths:
        temporary = f"{path}.arc-patch-tmp"
        try:
            _write_text(temporary, originals[path])
            os.replace(temporary, path)
        except OSError:
            _remove([temporary])
            failed.append(os.path.basename(path))
    return "" if not failed else f"; rollback also failed for {', '.join(failed)}"


def _write_changes(updates: dict[str, str], originals: dict[str, str]) -> str | None:
    """Swap in new file contents, or leave every target as it was.

    Returns ``None`` on success, otherwise a short reason. Each replacement is
    staged to a sibling file first and only swapped in once every one of them
    exists, so a failure while producing content never touches a target. A
    failure while swapping rolls the already-swapped targets back from the
    contents read before the patch, which makes the all-or-nothing contract true
    rather than merely intended.
    """

    staged: list[tuple[str, str]] = []
    for path, text in updates.items():
        temporary = f"{path}.arc-patch-tmp"
        try:
            _write_text(temporary, text)
        except OSError as exc:
            _remove([temporary, *(temporary for temporary, _ in staged)])
            return f"{os.path.basename(path)} could not be staged: {exc}"
        staged.append((temporary, path))

    for index, (temporary, path) in enumerate(staged):
        try:
            os.replace(temporary, path)
        except OSError as exc:
            _remove([temp for temp, _ in staged[index:]])
            return (
                f"{os.path.basename(path)} could not be replaced: {exc}"
                + _restore([target for _, target in staged[:index]], originals)
            )

    return None


def apply_template_patches(workspace_path: str, template_id: str) -> list[PatchOutcome]:
    """Apply the registered patches to a freshly copied workspace.

    Returns one outcome per patch: applied, already applied, unrecognized (a
    target exists but matches no known shape, or a prerequisite is missing), or
    skipped (the template ships none of the patch's target files, so there is
    nothing to patch). The caller decides how loudly to report each case.

    Classification happens before anything is written, only the files a patch
    actually changes are written, and those writes are staged and swapped
    together - so a workspace is never left in a state that is neither the old
    nor the new one.
    """

    outcomes: list[PatchOutcome] = []
    resolved: dict[str, str] = {}
    for patch in patches_for(template_id):
        skipped_prerequisites = [name for name in patch.requires if resolved.get(name) == SKIPPED]
        missing_prerequisites = [
            prerequisite
            for prerequisite in patch.requires
            if resolved.get(prerequisite) not in {APPLIED, ALREADY_APPLIED}
            and prerequisite not in skipped_prerequisites
        ]
        if skipped_prerequisites:
            # The prerequisite's targets are absent from this template, so the
            # dependent's are too (they edit the same files); there is nothing
            # to patch here either.
            outcomes.append(
                PatchOutcome(
                    patch.name,
                    SKIPPED,
                    "prerequisite patch(es) had no target files either: "
                    + ", ".join(skipped_prerequisites),
                )
            )
            resolved[patch.name] = SKIPPED
            continue
        if missing_prerequisites:
            # A prerequisite that shipped but did not resolve (unrecognized,
            # or a write failure) means this patch's search shapes cannot be
            # trusted either; classify it as unrecognized so the scaffold
            # stops rather than compiling half-fixed files.
            outcomes.append(
                PatchOutcome(
                    patch.name,
                    UNRECOGNIZED,
                    "prerequisite patch(es) were neither applied nor present: "
                    + ", ".join(missing_prerequisites),
                )
            )
            continue

        originals: dict[str, str] = {}
        patched: dict[str, str] = {}
        unavailable: set[str] = set()
        failures: list[str] = []
        seen_any_target = False

        for edit in patch.edits:
            path = os.path.join(workspace_path, *edit.relative_path.split("/"))
            if path in unavailable:
                continue
            if path not in originals:
                if not os.path.isfile(path):
                    unavailable.add(path)
                    failures.append(f"{edit.relative_path} is missing")
                    continue
                seen_any_target = True
                try:
                    originals[path] = _read_text(path)
                except OSError as exc:
                    unavailable.add(path)
                    failures.append(f"{edit.relative_path} could not be read: {exc}")
                    continue
            current = patched.get(path, originals[path])
            status, detail = _classify(current, edit)
            if status == UNRECOGNIZED:
                failures.append(detail)
            elif status == APPLIED:
                patched[path] = _patched_text(current, edit)

        if not seen_any_target:
            outcomes.append(
                PatchOutcome(patch.name, SKIPPED, "no target files present in this template")
            )
            resolved[patch.name] = SKIPPED
            continue
        if failures:
            outcomes.append(PatchOutcome(patch.name, UNRECOGNIZED, "; ".join(failures)))
            continue
        if not patched:
            outcomes.append(
                PatchOutcome(patch.name, ALREADY_APPLIED, "; ".join(edit.relative_path for edit in patch.edits))
            )
            resolved[patch.name] = ALREADY_APPLIED
            continue

        error = _write_changes(patched, originals)
        if error:
            outcomes.append(PatchOutcome(patch.name, UNRECOGNIZED, error))
            continue
        outcomes.append(
            PatchOutcome(patch.name, APPLIED, "; ".join(edit.relative_path for edit in patch.edits))
        )
        resolved[patch.name] = APPLIED

    return outcomes