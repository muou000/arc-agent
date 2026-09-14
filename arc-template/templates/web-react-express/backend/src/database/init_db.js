const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();

const DEFAULT_DB_FILENAME = 'database.db';

// Upper bound for initializeDatabase() attempts when a concurrent closeDb() /
// setDbPath() invalidates an in-flight initialization. Genuine init errors
// still surface (rethrown) once attempts are exhausted.
const MAX_INIT_ATTEMPTS = 5;

let db = null;
let initPromise = null;
let currentDbPath = resolveDbPath(
  process.env.ARC_DB_FILE || process.env.DATABASE_FILE || DEFAULT_DB_FILENAME,
);

function resolveDbPath(inputPath = DEFAULT_DB_FILENAME) {
  const candidate = String(inputPath || DEFAULT_DB_FILENAME).trim() || DEFAULT_DB_FILENAME;
  return path.resolve(process.cwd(), candidate);
}

function ensureDbDirectory(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function getDbPath() {
  return currentDbPath;
}

async function setDbPath(nextPath) {
  const resolvedPath = resolveDbPath(nextPath);
  if (resolvedPath === currentDbPath) {
    return currentDbPath;
  }

  await closeDb();
  currentDbPath = resolvedPath;
  return currentDbPath;
}

function getDb() {
  if (!db) {
    ensureDbDirectory(currentDbPath);
    db = new sqlite3.Database(currentDbPath);
  }
  return db;
}

function runStatement(database, sql) {
  return new Promise((resolve, reject) => {
    database.run(sql, (err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

function startInit() {
  const database = getDb();
  const promise = (async () => {
    await runStatement(database, 'PRAGMA foreign_keys = ON;');

    /**
     * Guide model instructions:
     * 1. Use CREATE TABLE IF NOT EXISTS to create new tables.
     * 2. When adding fields, use ALTER TABLE ... ADD COLUMN ... and guard it with existence checks or tolerant error handling.
     * 3. Keep schema evolution idempotent and centralized in this file.
     * 4. Reuse `db_runtime.js` for CRUD helpers and `test_harness.js` for test DB lifecycle instead of re-implementing one-off connection logic elsewhere.
     */

    return database;
  })();
  // If a concurrent closeDb() invalidates the handle mid-init, absorb the
  // rejection so it cannot become an unhandled rejection; initializeDatabase()
  // callers still observe it through their own await and retry against the
  // current generation.
  promise.catch(() => {});
  initPromise = promise;
  return promise;
}

async function initializeDatabase(options = {}) {
  if (options.dbPath) {
    await setDbPath(options.dbPath);
  }
  if (options.reset) {
    await resetDatabaseFile();
  }

  // Invariant: every resolved return value is an open handle for the current
  // generation. A concurrent closeDb()/setDbPath() can invalidate an in-flight
  // init; re-validate before handing the handle out and retry against the
  // current state instead of silently returning a closed database (which made
  // the next DB operation fail with "SQLITE_MISUSE: Database is closed").
  let lastError = null;
  for (let attempt = 0; attempt < MAX_INIT_ATTEMPTS; attempt += 1) {
    const pending = initPromise;
    if (pending) {
      try {
        const database = await pending;
        if (db === database) {
          return database;
        }
        // Generation was swapped while waiting; fall through and re-check.
      } catch (error) {
        lastError = error;
        if (initPromise === pending) {
          initPromise = null;
        }
      }
      continue;
    }

    try {
      const database = await startInit();
      if (db === database) {
        return database;
      }
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError || new Error('Database initialization did not produce a usable handle');
}

function closeDb() {
  const pendingInit = initPromise;
  initPromise = null;
  if (pendingInit) {
    // The in-flight init may reject with SQLITE_MISUSE once its handle is
    // closed underneath it. Absorb that rejection here so it never surfaces
    // as an unhandled rejection; initializeDatabase() awaiters observe the
    // same error through their own await and retry against the current state.
    pendingInit.catch(() => {});
  }
  if (!db) {
    return Promise.resolve();
  }

  const currentDb = db;
  db = null;
  return new Promise((resolve, reject) => {
    currentDb.close((err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

async function removeDatabaseFile(targetPath = currentDbPath) {
  const resolvedPath = resolveDbPath(targetPath);
  if (resolvedPath === currentDbPath) {
    await closeDb();
  }
  if (fs.existsSync(resolvedPath)) {
    fs.rmSync(resolvedPath, { force: true });
  }
}

async function resetDatabaseFile(targetPath = currentDbPath) {
  await removeDatabaseFile(targetPath);
}

module.exports = {
  DEFAULT_DB_FILENAME,
  resolveDbPath,
  getDbPath,
  setDbPath,
  getDb,
  initializeDatabase,
  closeDb,
  removeDatabaseFile,
  resetDatabaseFile,
};
