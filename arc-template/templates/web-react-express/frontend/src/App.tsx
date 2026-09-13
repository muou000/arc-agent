import type { ComponentType, ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

type PageModule = { default: ComponentType; route?: unknown };
type ProviderModule = { default: ComponentType<{ children?: ReactNode }> };

// Pages register themselves by exporting `route` next to the default component,
// and providers by being default-export components in `src/providers/`. This
// file is template-owned glue: never edit it to add a page or provider. Test
// assets are excluded from the globs so a misplaced test file cannot leak into
// the app bundle; modules without the expected export are ignored.
const pageModules = import.meta.glob(
  [
    './pages/**/*.tsx',
    '!./pages/**/__tests__/**',
    '!./pages/**/*.test.tsx',
    '!./pages/**/*.spec.tsx',
  ],
  { eager: true },
) as Record<string, PageModule>;
const providerModules = import.meta.glob(
  ['./providers/*.tsx', '!./providers/*.test.tsx', '!./providers/*.spec.tsx'],
  { eager: true },
) as Record<string, ProviderModule>;

interface PageDefinition {
  file: string;
  route: string;
  component: ComponentType;
}

function routeSpecificity(route: string): number[] {
  return route
    .split('/')
    .filter(Boolean)
    .map((segment) => (segment === '*' ? 2 : segment.startsWith(':') ? 1 : 0));
}

function compareRoutes(a: string, b: string): number {
  const left = routeSpecificity(a);
  const right = routeSpecificity(b);
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const diff = (left[index] ?? 0) - (right[index] ?? 0);
    if (diff !== 0) {
      return diff;
    }
  }
  return a.localeCompare(b);
}

// Pages register themselves by exporting `route` next to the default component,
// and providers by being default-export components in `src/providers/`. This
// file is template-owned glue: never edit it to add a page or provider.
const pages = Object.entries(pageModules)
  .map<PageDefinition | null>(([file, module]) =>
    typeof module?.route === 'string' && typeof module?.default === 'function'
      ? { file, route: module.route, component: module.default }
      : null,
  )
  .filter((page): page is PageDefinition => page !== null)
  .sort((a, b) => compareRoutes(a.route, b.route));

const providers = Object.entries(providerModules)
  .filter(([, module]) => typeof module?.default === 'function')
  .sort(([a], [b]) => a.localeCompare(b))
  .map(([, module]) => module.default);

function App() {
  let tree = (
    <Routes>
      {pages.map((page) => {
        const Page = page.component;
        return <Route key={page.file} path={page.route} element={<Page />} />;
      })}
    </Routes>
  );

  for (const Provider of providers) {
    tree = <Provider>{tree}</Provider>;
  }

  return tree;
}

export default App;
