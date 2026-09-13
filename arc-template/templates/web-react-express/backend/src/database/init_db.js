const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();

const DEFAULT_DB_FILENAME = 'database.db';

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

const SCHEMA_MODULE_PATTERN = /\.schema\.js$/;
const SCHEMA_MODULE_DIR = path.join(__dirname, 'schema');

// Feature schema modules (`database/schema/<feature>.schema.js`) are
// contributed by requirement nodes and loaded automatically in ascending
// `order` (default 100), then filename order. Each module exports an
// idempotent `async apply(db)` that creates its tables and seed data with
// IF NOT EXISTS style guards. Never edit this file to evolve the schema:
// add a schema module instead.
function loadSchemaModules() {
  if (!fs.existsSync(SCHEMA_MODULE_DIR)) {
    return [];
  }
  return fs
    .readdirSync(SCHEMA_MODULE_DIR)
    .filter((name) => SCHEMA_MODULE_PATTERN.test(name))
    .sort()
    .map((name) => {
      const module = require(path.join(SCHEMA_MODULE_DIR, name));
      if (!module || typeof module.apply !== 'function') {
        throw new Error(`Schema module ${name} must export an async apply(db) function.`);
      }
      const order = Number.isFinite(module.order) ? module.order : 100;
      return { name, module, order };
    })
    .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
}

async function initializeDatabase(options = {}) {
  if (options.dbPath) {
    await setDbPath(options.dbPath);
  }
  if (options.reset) {
    await resetDatabaseFile();
  }
  if (initPromise) {
    // Memoized path: callers await this function and then use the result as a
    // database handle. Returning the init promise would hand them a
    // Promise<void>, so every second DB operation failed with
    // "Cannot read properties of undefined (reading 'exec')".
    await initPromise;
    return getDb();
  }

  const database = getDb();
  initPromise = (async () => {
    await runStatement(database, 'PRAGMA foreign_keys = ON;');

    /**
     * Guide model instructions:
     * 1. Add per-feature schema modules under `database/schema/<feature>.schema.js`
     *    exporting an idempotent `async apply(db)`; they are loaded automatically.
     * 2. Guard every statement inside the module (CREATE TABLE IF NOT EXISTS,
     *    tolerant ALTER TABLE checks) so repeated startups stay idempotent.
     * 3. Reuse `db_runtime.js` for CRUD helpers and `test_harness.js` for test DB
     *    lifecycle instead of re-implementing one-off connection logic elsewhere.
     */
    for (const { module } of loadSchemaModules()) {
      await module.apply(database);
    }
  })();

  try {
    await initPromise;
  } catch (error) {
    initPromise = null;
    throw error;
  }

  return database;
}

function closeDb() {
  if (!db) {
    initPromise = null;
    return Promise.resolve();
  }

  const currentDb = db;
  db = null;
  initPromise = null;
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
