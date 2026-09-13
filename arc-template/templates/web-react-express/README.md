# Web Template

This template is a single-port web application. The frontend is built with Vite and React, and the backend is an Express server that serves both `/api/*` routes and the compiled frontend from `frontend/dist`.

## Project Layout

- `frontend/`: React, Vite, Tailwind, Vitest, and frontend tests.
- `backend/`: Express, SQLite runtime helpers, backend Vitest tests, and Playwright E2E tests.
- `backend/src/app.js`: Express app, `/api/health`, static frontend hosting, and SPA fallback. It does not register feature APIs directly: those are auto-mounted from `backend/src/routes/`.
- `backend/src/index.js`: Backend process entrypoint.

## Registration-Based Shared Glue

Shared composition files are assembled automatically from per-feature
registration modules, so concurrent contributors only ever add new files and
never edit the same glue file:

| Concern | Add a new module | Auto-assembled by | Never edit |
| --- | --- | --- | --- |
| Backend API | `backend/src/routes/<feature>.routes.js` exporting `{ mountPath: '/api/<feature>', router }` | `backend/src/routes/index.js` (mounted in filename order) | `backend/src/app.js`, `backend/src/routes/index.js` |
| Database schema & seed | `backend/src/database/schema/<feature>.schema.js` exporting an idempotent `async apply(db)` and an optional numeric `order` (default 100) | `backend/src/database/init_db.js` on every startup | `backend/src/database/init_db.js` |
| Frontend page | `frontend/src/pages/<Page>.tsx` exporting the default component plus `export const route = '<path>'` | `frontend/src/App.tsx` (static segments before params before wildcards) | `frontend/src/App.tsx`, `frontend/src/main.tsx` |
| Home page region | `frontend/src/sections/home/<Section>.tsx` exporting the default component plus an optional `export const sectionOrder` | `frontend/src/pages/HomePage.tsx` (lower `sectionOrder` renders first) | `frontend/src/pages/HomePage.tsx` |
| Global provider | `frontend/src/providers/<Name>Provider.tsx` exporting a default provider component wrapping `children` | `frontend/src/App.tsx` (filename order, outermost first) | `frontend/src/main.tsx`, `frontend/src/App.tsx` |

Per-feature API calls should import the shared axios client from
`frontend/src/api/index.ts` inside a new module such as
`frontend/src/api/<feature>.ts`; the client file itself stays template-owned,
as does `frontend/src/index.css` for global styles.

## Prerequisites

- Node.js and npm.
- A shell environment that can install npm dependencies in both `frontend` and `backend`.

## Install Dependencies

Install each package independently:

```bash
cd backend
npm install

cd ../frontend
npm install
```

## Build and Run

The production-style runtime is backend-led. Build the frontend first, then start the backend:

```bash
cd frontend
npm run build

cd ../backend
npm run start
```

The backend listens on `PORT` when set, otherwise it uses the template port configured by ARC. After startup, the backend serves the compiled frontend and API routes from the same origin.

Health check:

```bash
curl http://127.0.0.1:<port>/api/health
```

## Development Commands

Backend development server:

```bash
cd backend
npm run dev
```

Frontend development server:

```bash
cd frontend
npm run dev
```

The Vite dev server proxies `/api` to the configured backend port.

## Tests

Frontend unit/integration tests:

```bash
cd frontend
npm run test
```

Backend unit/integration tests:

```bash
cd backend
npm run test
```

Backend E2E tests use Playwright and live under `backend/test-e2e`:

```bash
cd backend
npm run test:e2e
```

Before the first E2E run the Playwright browser binaries must be downloaded
(`npm install` only installs the runner):

```bash
cd backend
npm run e2e:install-browsers
```

This installs both `chromium` and `chromium-headless-shell` (Playwright 1.5x
launches the headless shell in default headless mode), and the download is
machine-wide, so it is paid once per machine.

Run backend Vitest tests followed by E2E tests:

```bash
cd backend
npm run test:all
```

## Database Notes

- Runtime database helpers live under `backend/src/database`.
- The default database file is `database.db`, unless `ARC_DB_FILE` or `DATABASE_FILE` is set.
- Test helpers create isolated SQLite files under `.arc-test-db`.
- `npm run db:seed` runs the template seed entrypoint.
- `npm run db:prepare:e2e` prepares an isolated E2E database.
