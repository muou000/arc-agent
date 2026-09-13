import type { ComponentType } from 'react';

type SectionModule = { default: ComponentType; sectionOrder?: unknown };

// Test assets are excluded so a misplaced test file cannot leak into the app
// bundle; modules without a default export are ignored.
const sectionModules = import.meta.glob(
  ['../sections/home/*.tsx', '!../sections/home/*.test.tsx', '!../sections/home/*.spec.tsx'],
  { eager: true },
) as Record<string, SectionModule>;

interface SectionDefinition {
  file: string;
  order: number;
  component: ComponentType;
}

// Home page regions register themselves from `src/sections/home/`: each section
// is a default-exported component with an optional `sectionOrder` (lower renders
// first). Never edit this file to place content on the home page: add a section
// component instead.
const sections = Object.entries(sectionModules)
  .map<SectionDefinition | null>(([file, module], index) =>
    typeof module?.default === 'function'
      ? {
          file,
          component: module.default,
          order: typeof module.sectionOrder === 'number' ? module.sectionOrder : index,
        }
      : null,
  )
  .filter((section): section is SectionDefinition => section !== null)
  .sort((a, b) => a.order - b.order || a.file.localeCompare(b.file));

function HomePage() {
  return (
    <main className="min-h-screen">
      {sections.map(({ file, component: Section }) => (
        <Section key={file} />
      ))}
    </main>
  );
}

export const route = '/';

export default HomePage;
