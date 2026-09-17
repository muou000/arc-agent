---
name: web-test-harness-skill
description: Use for ARC TestGenerator work on web-template nodes (Express + React + SQLite) that write Vitest or Playwright tests. Provides proven recipes against the preinstalled harness, isolated-database lifecycle, supertest import order, frontend render helpers, E2E uniqueness conventions, so the first leaf starts from established patterns instead of exploring or rebuilding test infrastructure.
---

# web-test-harness-skill

Use this skill when generating Unit, Integration, or E2E tests inside the web template workspace (`backend/` Express + SQLite, `frontend/` React + Vite).

## The harness is already installed — never rebuild it

The template ships the complete test infrastructure. Treat these as fixed contracts:

- Backend Vitest: `backend/vitest.config.js` includes `backend/tests/**` and excludes `backend/test-e2e/**`.
- Playwright: `backend/playwright.config.js` runs `backend/test-e2e/**` with `baseURL` from `PLAYWRIGHT_BASE_URL` / `ARC_WEB_BASE_URL` (default `http://127.0.0.1:3000`).
- Frontend Vitest: `frontend/vite.config.js` embeds jsdom, `globals: true`, and `frontend/test/setup.ts` (jest-dom matchers plus automatic cleanup).
- Isolated test database: `backend/src/database/test_harness.js` exports `createTestDatabaseHarness`.
- All runner libraries (`vitest`, `supertest`, `@testing-library/*`, `@playwright/test`) are already declared in both `package.json` files.

These files are injected into your context as `<scaffold_files>`. The bullets above are starting anchors, not the contract itself: the durable contract is the usage pattern (setup -> operate -> cleanup lifecycle, import ordering, placement rules below). When a recipe references a path, export, or option that does not match the actual scaffold file contents, trust the file and adapt the recipe to it — never invent the missing piece, and never rebuild infrastructure to match the recipe.

Rules:

1. Never create or edit a test config, setup file, harness module, or test-related `package.json` entry (test scripts, test `devDependencies`) to "set up" testing, and never install dependencies for it. This boundary covers test infrastructure only: when the application source under test genuinely needs a new runtime dependency, adding it to `dependencies` is the implementer's call in the later TDD stage, not a harness concern. If a recipe below works against the files listed above, the test infrastructure is sufficient.
2. Do not rely on runner globals. Load Vitest with ESM `import { describe, it, expect, vi } from 'vitest'` even in the CommonJS backend package; load Playwright with `const { test, expect } = require('@playwright/test')`.
3. Lock the final test file list (path, type, covered interface ids) before the first write; every subsequent write must land on one of those paths. Never rename, re-create, or delete-then-rewrite a test file mid-pass — pick the final name once and fix content in place.
4. Name files after the module under test, not after requirement ids or scenario prose: `backend/tests/<domain>Repository.test.js` mirrors `backend/src/repositories/<domain>Repository.js`; a frontend file containing JSX ends in `.test.tsx`/`.spec.tsx`, one without JSX in `.test.ts`; Playwright files end in `.e2e.spec.js`. Use `.test.<ext>` consistently for Vitest files.
5. One Vitest file per owned executable capability per layer; do not split a capability into many files or reorganize by renaming. Open each `describe` with `'<Module> (<interface-ids>)'` so tests stay traceable to the interface contract.
6. Centralize valid input construction in one factory per file — `function valid<X>(overrides = {}) { return { ...all required fields..., ...overrides }; }` — and let each test override only the field it varies. Never copy a ten-field payload into every `it`.
7. Pick the recipe by the interface type under test, not by the domain: repository/service → backend unit; route/boundary wiring → backend API integration; page/component → frontend component; pure logic → frontend unit; user-visible scenario → E2E.

## Recipe — backend unit (repository / service)

```js
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createTestDatabaseHarness } from '../src/database/test_harness';
import * as domainRepository from '../src/repositories/domainRepository';

describe('DomainRepository (<interface-ids>)', () => {
  let harness;

  beforeEach(async () => {
    harness = createTestDatabaseHarness({ label: 'domain-repository' });
    await harness.setup();
  });

  afterEach(async () => {
    await harness.cleanup();
  });
});
```

8. `harness.setup()` creates a fresh throwaway SQLite file, `cleanup()` closes and deletes it and restores the previous database path. Create one harness per test inside `beforeEach`/`afterEach`; never share a database between tests.
9. Build preconditions through the real application path when it exists — call the real service or repository in setup — instead of raw SQL inserts or a fixture DSL.
10. Assert persisted outcomes through the module's own read APIs, and after asserting a rejected write also assert no residue was left behind (re-query and expect null/absent).

## Recipe — backend API integration (supertest)

```js
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import request from 'supertest';
import { createTestDatabaseHarness } from '../src/database/test_harness';

let harness;
let app;

beforeAll(async () => {
  harness = createTestDatabaseHarness({ label: 'domain-api' });
  await harness.setup();
  // Order contract: harness.setup() must redirect the database path BEFORE
  // anything imports code that initializes the database. Dynamic import is
  // the mechanism that guarantees this ordering.
  app = (await import('../src/app')).default;
});

afterAll(async () => {
  await harness.cleanup();
});
```

11. The ordering contract is `harness.setup()` before any import or call that can trigger database initialization. In the current template that means a dynamic `await import('../src/app')`, because `backend/src/app.js` initializes the database at module load; if initialization ever becomes explicit or lazy, the contract still applies and only the mechanism changes. A static top-level import is the canonical failure — the app binds to the wrong database file and tests silently write outside the isolated database.
12. Assert `response.status`, the response envelope, and user-visible messages in the requirement's language (for Chinese requirements match `/中文关键词/`), never raw error stack text.
13. For cookie flows, extract once and replay it: `const cookie = res.headers['set-cookie'].find((c) => c.includes('<cookie-name>=')).split(';')[0];` then `.set('Cookie', cookie)`.

## Recipe — frontend component test

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import * as domainApi from '../src/api/domain';

function renderPage() {
  return render(
    <SessionProvider>
      <MemoryRouter initialEntries={['/domain']}>
        <Routes>
          <Route path="/domain" element={<DomainPage />} />
          <Route path="/" element={<div>HOME-STUB</div>} />
        </Routes>
      </MemoryRouter>
    </SessionProvider>,
  );
}
```

14. Wrap only in the providers and router the page actually consumes; when a test asserts navigation away, mount a stub route for the destination instead of the real page. Render through one local `renderPage()` helper per file, not repeated inline JSX.
15. Spy the application's own API boundary — `vi.spyOn(domainApi, 'fetchDomain').mockResolvedValue(...)` — never component internals or the fetch implementation. Reset with `vi.restoreAllMocks()` in `beforeEach`.
16. Prefer role and label queries (`screen.findByRole`, `getByLabel`) with the requirement-declared accessible names, and `waitFor`/`findBy*` for async outcomes. Assert what the user sees, not that a mock was called.

## Recipe — Playwright E2E

```js
const { test, expect } = require('@playwright/test');

function uniqueSuffix() {
  return crypto.randomUUID().slice(0, 8);
}
```

17. Every test that creates records derives its identifiers from `uniqueSuffix()` (usernames, emails, document numbers); fixed values collide across runs, across parallel workers, and with seeded data. `crypto.randomUUID()` is globally available in the Playwright Node runtime and safe under multi-worker execution, unlike millisecond timestamps.
18. Select from requirement-declared labels, roles, and routes first — `page.getByLabel('用户名')`, `page.getByRole('button', { name: '下一步' })` — and scope or use `exact: true` when one label is a substring of another. Assert visible outcomes in the requirement's language and URL changes with `expect(page).toHaveURL(/\/route$/)`.
19. The system starts the backend-hosted app and prepares the E2E database; tests must not spawn servers, call database preparation scripts, write to the database directly, or define custom fixtures for those concerns.
20. Verify session or global state through the real API surface that shares the browser context: `const res = await page.request.get('/api/<state-endpoint>');` then assert on the JSON body.

## Anti-patterns

- Creating a harness, config, setup file, or dependency to "prepare" testing instead of following a recipe against the installed files.
- Renaming, delete-recreating, or writing numbered variants (`x2`, `x3`) of a test file; writing `.ts` bridge files that import `.tsx` tests.
- Static `import app` before the database harness redirected the path; sharing one database file across tests in a suite.
- Mocking the database or service layer to avoid the harness, or mocking fetch internals instead of spying the app's own API module.
- Fixed identifiers in E2E; asserting error text in a language the requirement does not use.
