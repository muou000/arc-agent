const fs = require('fs');
const path = require('path');

const ROUTE_MODULE_PATTERN = /\.routes\.js$/;

// Each `*.routes.js` file in this directory is an independent feature router
// contributed by one requirement node. A module exports
// `{ mountPath: string, router: ExpressRouter }` and is mounted automatically
// in filename order. Never edit this loader or `app.js` to wire a new API:
// add a route module instead.
function loadRouteModules() {
  return fs
    .readdirSync(__dirname)
    .filter((name) => ROUTE_MODULE_PATTERN.test(name))
    .sort()
    .map((name) => {
      const module = require(path.join(__dirname, name));
      if (!module || typeof module.mountPath !== 'string' || !module.router) {
        throw new Error(
          `Route module ${name} must export { mountPath: string, router: ExpressRouter }.`
        );
      }
      return module;
    });
}

function registerRoutes(app) {
  for (const { mountPath, router } of loadRouteModules()) {
    app.use(mountPath, router);
  }
}

module.exports = { registerRoutes };
