"""Directed, marker-guarded fixes for the platform-provisioned app templates.

A run compiles against the templates the platform provisions through
``ARC_AGENT_TEMPLATES_ROOT`` (see ``app_type_handler/base.py``); the in-repo
``arc-template/templates`` tree only mirrors them and carries no repo-only
fixes. A fix that must reach every generated workspace therefore cannot live
as an edit to the template source - the provisioned template would never carry
it, every workspace would be generated from the unfixed files, and every node
would pay for the defect in its own TDD loop. Fixes are delivered here instead
and applied to the workspace immediately after the template is copied.

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
from contextlib import suppress
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
_TEST_HARNESS_PATH = "backend/src/database/test_harness.js"
_README_PATH = "README.md"
_BACKEND_GITIGNORE_PATH = "backend/.gitignore"
_BACKEND_APP_PATH = "backend/src/app.js"

# PR #28 fixed two defects in the template's database bootstrap, and this
# chain also carries the earlier handle-return fix (a15ad24) the provisioned
# template never received. All three are delivered here instead of as repo
# template edits, because the template that actually compiles the workspace is
# provisioned by the platform, and the in-repo mirror tracks that official
# template rather than carrying repo-only fixes:
#
#  0. initializeDatabase() returned the bare init promise from the memoized
#     path, handing callers a Promise<void> instead of a handle, so every
#     second DB operation failed with
#     "Cannot read properties of undefined (reading 'exec')".
#  1. initializeDatabase() could hand a caller a closed handle when a
#     concurrent closeDb()/setDbPath() invalidated an in-flight first init, so
#     the caller's next DB operation failed with
#     "SQLITE_MISUSE: Database is closed", and the interrupted init could
#     surface as an unhandled rejection.
#  2. A genuine init failure was swallowed into the bounded retry loop instead
#     of surfacing, so a real error burned every attempt before being reported.
#
# The search shapes below assume the official template's pre-fix content. When
# the platform re-provisions a newer official template, re-align the mirror and
# these shapes together - a search shape that only matches the repo mirror
# breaks the scaffold on every online run (seen 2026-09-18: the shapes assumed
# an a15ad24-era template the platform had never shipped).
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
                    "    return initPromise;\n"
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
                    "- `initializeDatabase()` always resolves to an open handle for the current database path, even when `closeDb()`/`setDbPath()` race an in-flight initialization; it never returns a closed handle. The provisioned template may predate this fix; ARC applies it to the workspace at scaffold time via `app_type_handler/template_patches.py`.\n"
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
    # The easy-ticketbooking run of 2026-09-21 lost ~1.5 minutes to a flaky
    # SQLITE_CANTOPEN {errno: 14} (vitest unhandled error, Unit layer, attempt
    # 2 of a TDD loop; self-healed over three retry rounds without a source
    # change). Every harness wrote its sqlite file into one shared
    # `.arc-test-db` root, and each cleanup removed that root when it was
    # empty - so one worker's cleanup could delete the directory between
    # another worker's mkdir and the open node-sqlite3 had already queued (the
    # file only materializes when the queued open executes, and no later mkdir
    # can repair a directory deleted in that window). Two directed fixes:
    #
    #  0. each harness opens its database inside a dedicated scope directory
    #     (`<root>/<label>-<suffix>/`), so cleanup removes only a directory it
    #     alone owns and the shared root is never removed by anyone;
    #  1. reset() re-creates the root directory before reopening sqlite: it
    #     can run long after setup() and must not assume that mkdir still
    #     holds.
    #
    # The search shapes assume the official template's pre-fix content; see
    # the init-db comment above for the realignment discipline.
    TemplatePatch(
        name="test-harness-temp-dir-guard",
        template_id="web-react-express",
        summary=(
            "each test database harness opens sqlite inside its own scope "
            "directory and reset() re-creates the root before reopening, so a "
            "concurrent cleanup can no longer race a queued sqlite open"
        ),
        edits=(
            TemplateEdit(
                relative_path=_TEST_HARNESS_PATH,
                search=(
                    "  const uniqueSuffix = `${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;\n"
                    "  return {\n"
                    "    dbPath: path.join(rootDir, `${label}-${uniqueSuffix}.sqlite`),\n"
                    "    rootDir,\n"
                    "    preserveDatabaseOnCleanup,\n"
                    "    removeRootDirWhenEmpty: true,\n"
                    "  };\n"
                ),
                replace=(
                    "  const uniqueSuffix = `${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;\n"
                    "  // A dedicated scope directory per harness: cleanup then removes only a\n"
                    "  // directory this harness alone owns, so a concurrent cleanup can never\n"
                    "  // delete the directory another worker is between mkdir and its queued\n"
                    "  // sqlite open (SQLITE_CANTOPEN, errno 14).\n"
                    "  const scopeDir = path.join(rootDir, `${label}-${uniqueSuffix}`);\n"
                    "  return {\n"
                    "    dbPath: path.join(scopeDir, `${label}-${uniqueSuffix}.sqlite`),\n"
                    "    rootDir: scopeDir,\n"
                    "    preserveDatabaseOnCleanup,\n"
                    "    removeRootDirWhenEmpty: true,\n"
                    "  };\n"
                ),
                applied_marker="const scopeDir = path.join(rootDir,",
            ),
            TemplateEdit(
                relative_path=_TEST_HARNESS_PATH,
                search=(
                    "  async function reset(seedHook) {\n"
                    "    await ensureHarnessIsActive();\n"
                    "    await resetDatabaseFile();\n"
                    "    await initializeDatabase();\n"
                ),
                replace=(
                    "  async function reset(seedHook) {\n"
                    "    await ensureHarnessIsActive();\n"
                    "    // Re-create the root directory before reopening sqlite: reset() can run\n"
                    "    // long after setup() and must not assume the one setup() made still exists.\n"
                    "    fs.mkdirSync(rootDir, { recursive: true });\n"
                    "    await resetDatabaseFile();\n"
                    "    await initializeDatabase();\n"
                ),
                applied_marker="Re-create the root directory before reopening sqlite",
            ),
        ),
    ),
    # `npx playwright test` writes its volatile artifacts (traces, videos,
    # `.last-run.json`) into `backend/test-results/` and
    # `backend/playwright-report/` by default; the template's backend
    # .gitignore ignores neither. Two costs: every phase checkpoint
    # (`git add -A`) commits them, and every parallel sibling merge carries
    # them as changed files - the 2026-09-22 rebase-on-merge benchmark saw
    # `backend/test-results/.last-run.json` inside a four-file merge
    # conflict set. A live dev server holding a trace file open is also the
    # Windows lock shape that can block a mid-phase replay's git operations.
    # The fix is the ignore entry every Playwright scaffold carries.
    TemplatePatch(
        name="gitignore-playwright-artifacts",
        template_id="web-react-express",
        summary=(
            "backend .gitignore ignores Playwright's volatile test-results/ and "
            "playwright-report/ output directories, keeping runner artifacts out "
            "of checkpoints, merges and replays"
        ),
        edits=(
            TemplateEdit(
                relative_path=_BACKEND_GITIGNORE_PATH,
                search=(
                    "node_modules\n"
                    "*.db\n"
                    ".arc-test-db\n"
                    ".env\n"
                    "coverage\n"
                ),
                replace=(
                    "node_modules\n"
                    "*.db\n"
                    ".arc-test-db\n"
                    ".env\n"
                    "coverage\n"
                    "test-results\n"
                    "playwright-report\n"
                ),
                applied_marker="playwright-report",
            ),
        ),
    ),
    # The Web SPA fallback serves the built shell with
    # `res.sendFile(<absolute dist path>/index.html)`. send (express's file
    # server) applies its dotfile policy to the *absolute* target path of a
    # root-less sendFile, and its default policy (`dotfiles: 'ignore'`)
    # rejects any target crossing a dot-directory with a synchronous 404 -
    # before the filesystem is ever consulted. Per-stage worktrees always
    # live under `.arc/stage-worktrees/...`, so every SPA page navigation in
    # such a workspace 404s: each E2E test burns its full timeout waiting for
    # a UI that never mounts and the batch dies at the runner cap with its
    # output discarded (the 2026-09-26 easy-ticketbooking run paid five
    # 120s Playwright batches this way). `express.static` is unaffected -
    # its root-based path only checks the request-path segments - so the
    # defect is invisible until an agent-driven page navigation hits the
    # fallback. `dotfiles: 'allow'` re-enables exactly this fixed target;
    # the request path never reaches sendFile, so the traversal concern the
    # policy guards against does not apply.
    TemplatePatch(
        name="spa-fallback-dotfile-policy",
        template_id="web-react-express",
        summary=(
            "SPA fallback sendFile opts into send's dotfiles:'allow' so the "
            "fixed index.html target still serves when the workspace path "
            "crosses a dot-directory (.arc/stage-worktrees/...)"
        ),
        edits=(
            TemplateEdit(
                relative_path=_BACKEND_APP_PATH,
                search=(
                    "    res.sendFile(path.join(frontendDistPath, 'index.html'));\n"
                ),
                replace=(
                    "    // `dotfiles: 'allow'` is load-bearing: send applies its dotfile policy\n"
                    "    // to the absolute target path of a root-less sendFile, and its default\n"
                    "    // policy 404s any target crossing a dot-directory - the workspace lives\n"
                    "    // under `.arc/stage-worktrees/...` in per-stage worktree runs.\n"
                    "    res.sendFile(path.join(frontendDistPath, 'index.html'), { dotfiles: 'allow' });\n"
                ),
                applied_marker="{ dotfiles: 'allow' }",
            ),
        ),
    ),
    # The 2026-09-26 easy-ticketbooking arc-output2 run died at the merge
    # health gate: REQ-1's design skeleton exported named handler functions
    # while its app.js glue mounted the module as a router, and Express 5
    # threw "argument handler must be a function" at boot - a stage-terminal
    # failure the designing agent could still have fixed had it seen it. The
    # anchor comment puts the canonical mount idiom exactly where the glue
    # gets written, so the shared-surface edit carries its own contract.
    TemplatePatch(
        name="appjs-router-mount-guard",
        template_id="web-react-express",
        summary=(
            "app.js route-registration anchor states the mount contract: "
            "app.use()-mounted modules must be Express Routers or middleware "
            "functions, never plain objects of named handlers"
        ),
        edits=(
            TemplateEdit(
                relative_path=_BACKEND_APP_PATH,
                search=(
                    "// register routes\n"
                    "app.get('/api/health', (req, res) => {\n"
                ),
                replace=(
                    "// register routes\n"
                    "// Mounting route modules: a module imported under `// route modules imports`\n"
                    "// and mounted with app.use('<path>', mod) must itself be an Express Router\n"
                    "// (`const router = express.Router(); ...; module.exports = router;`) or a\n"
                    "// middleware function. Mounting a plain object of named handler functions\n"
                    "// throws at boot and fails the stage's backend health gate.\n"
                    "app.get('/api/health', (req, res) => {\n"
                ),
                applied_marker="// Mounting route modules:",
            ),
        ),
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
        with suppress(OSError):
            os.remove(path)


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
