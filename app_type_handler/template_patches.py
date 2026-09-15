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
  the file is reported and left untouched instead of being clobbered - a
  template that evolved upstream must not be silently overwritten.

A patch is applied atomically: if any of its edits cannot be classified, none
of them is written, so a workspace can never end up half-patched.
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


@dataclass(frozen=True)
class PatchOutcome:
    """What happened to one patch on one workspace."""

    patch_name: str
    status: str  # "applied" | "already-applied" | "unrecognized"
    detail: str


APPLIED = "applied"
ALREADY_APPLIED = "already-applied"
UNRECOGNIZED = "unrecognized"


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
    ),
)


def patches_for(template_id: str) -> tuple[TemplatePatch, ...]:
    """Patches registered for one template, in application order."""

    return tuple(patch for patch in TEMPLATE_PATCHES if patch.template_id == template_id)


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
    """Report how one edit relates to a file's current content."""

    if edit.applied_marker in text:
        return ALREADY_APPLIED, f"{edit.relative_path} already carries the fix"
    newline = "\r\n" if "\r\n" in text else "\n"
    search = edit.search if newline == "\n" else edit.search.replace("\n", "\r\n")
    occurrences = text.count(search)
    if occurrences == 0:
        return UNRECOGNIZED, (
            f"{edit.relative_path} contains neither the known pre-fix shape nor the fix marker"
        )
    if occurrences > 1:
        return UNRECOGNIZED, (
            f"{edit.relative_path} contains the known pre-fix shape {occurrences} times"
        )
    return APPLIED, edit.relative_path


def _patched_text(text: str, edit: TemplateEdit) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    search = edit.search if newline == "\n" else edit.search.replace("\n", "\r\n")
    replace = edit.replace if newline == "\n" else edit.replace.replace("\n", "\r\n")
    return text.replace(search, replace, 1)


def apply_template_patches(workspace_path: str, template_id: str) -> list[PatchOutcome]:
    """Apply the registered patches to a freshly copied workspace.

    Returns one outcome per patch. A patch is either applied, already applied,
    or left alone because its target no longer matches a known shape; the
    caller decides how loudly to report the last case. Nothing is written when
    any of a patch's edits is unclassifiable, so a partial patch cannot leave
    a workspace in a state that is neither the old nor the new one.
    """

    outcomes: list[PatchOutcome] = []
    for patch in patches_for(template_id):
        texts: dict[str, str] = {}
        failures: list[str] = []
        already = True

        for edit in patch.edits:
            path = os.path.join(workspace_path, *edit.relative_path.split("/"))
            if path not in texts:
                if not os.path.isfile(path):
                    failures.append(f"{edit.relative_path} is missing")
                    continue
                try:
                    texts[path] = _read_text(path)
                except OSError as exc:
                    failures.append(f"{edit.relative_path} could not be read: {exc}")
                    continue
            status, detail = _classify(texts[path], edit)
            if status == UNRECOGNIZED:
                failures.append(detail)
            elif status == APPLIED:
                already = False
                texts[path] = _patched_text(texts[path], edit)

        if failures:
            outcomes.append(PatchOutcome(patch.name, UNRECOGNIZED, "; ".join(failures)))
            continue
        if already:
            outcomes.append(
                PatchOutcome(patch.name, ALREADY_APPLIED, "; ".join(edit.relative_path for edit in patch.edits))
            )
            continue

        for path, text in texts.items():
            try:
                _write_text(path, text)
            except OSError as exc:
                outcomes.append(
                    PatchOutcome(patch.name, UNRECOGNIZED, f"{os.path.basename(path)} could not be written: {exc}")
                )
                break
        else:
            outcomes.append(
                PatchOutcome(patch.name, APPLIED, "; ".join(edit.relative_path for edit in patch.edits))
            )

    return outcomes