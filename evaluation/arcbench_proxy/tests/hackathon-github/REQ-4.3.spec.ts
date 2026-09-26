import { test, expect } from '@playwright/test';
import { FIXTURES, openRepository } from './helpers';

// covers: REQ-4-3-1
test('REQ-4-3-1: switching to feature-search reveals its branch-only file', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Branch main', exact: true }).click();
  const findBranch = page.getByRole('textbox', { name: 'Find branch', exact: true });
  await findBranch.fill('feature-search');
  await page.getByRole('option', { name: 'feature-search', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Branch feature-search', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'main-only.md', exact: true })).toBeVisible();
});

// covers: REQ-4-3-1
test('REQ-4-3-1: an unmatched search preserves the active branch across reload', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Branch main', exact: true }).click();
  const findBranch = page.getByRole('textbox', { name: 'Find branch', exact: true });
  await findBranch.fill('no-such-branch');
  await expect(page.getByText('No matching branch')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Branch main', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Branch main', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'main-only.md', exact: true })).toHaveCount(0);
});

// covers: REQ-4-3-1
test('REQ-4-3-1: switching away and back does not modify either branch listing', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Branch main', exact: true }).click();
  await page.getByRole('textbox', { name: 'Find branch', exact: true }).fill('feature-search');
  await page.getByRole('option', { name: 'feature-search', exact: true }).click();
  await expect(page.getByRole('link', { name: 'main-only.md', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Branch feature-search', exact: true }).click();
  await page.getByRole('textbox', { name: 'Find branch', exact: true }).fill('main');
  await page.getByRole('option', { name: 'main', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Branch main', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'main-only.md', exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'README.md', exact: true })).toBeVisible();
});

// covers: REQ-4-3-2
test('REQ-4-3-2: typing a valid unused name offers branch creation without Enter', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Branch main', exact: true }).click();
  const name = 'pw-branch-' + String(Date.now());
  const findBranch = page.getByRole('textbox', { name: 'Find branch', exact: true });
  await findBranch.fill(name);
  await page.getByRole('option', { name: `Create branch: ${name}`, exact: true }).click();
  await expect(page.getByRole('button', { name: `Branch ${name}`, exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: `Branch ${name}`, exact: true })).toBeVisible();
});

// covers: REQ-4-3-2
test('REQ-4-3-2: an invalid branch name is rejected immediately', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Branch main', exact: true }).click();
  const findBranch = page.getByRole('textbox', { name: 'Find branch', exact: true });
  await findBranch.fill('invalid..branch');
  await expect(page.getByText('Invalid branch')).toBeVisible();
  await expect(page.getByRole('option', { name: 'Create branch: invalid..branch', exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Branch main', exact: true })).toBeVisible();
});

// covers: REQ-4-3-2
test('REQ-4-3-2: an existing name offers selection instead of creation', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Branch main', exact: true }).click();
  const findBranch = page.getByRole('textbox', { name: 'Find branch', exact: true });
  await findBranch.fill('feature-search');
  await expect(page.getByRole('option', { name: 'feature-search', exact: true })).toBeVisible();
  await expect(page.getByRole('option', { name: 'Create branch: feature-search', exact: true })).toHaveCount(0);
});

// covers: REQ-4-3-3
test('REQ-4-3-3: an admin switches the default branch to release and restores main', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('link', { name: 'Branches', exact: true }).click();
  await page.getByRole('combobox', { name: 'Default branch', exact: true }).selectOption({ label: 'release' });
  await page.getByRole('button', { name: 'Update', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  await openRepository(page, FIXTURES.publicRepository);
  await expect(page.getByRole('button', { name: 'Branch release', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Branch release', exact: true }).click();
  await expect(page.getByRole('option', { name: 'main', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('link', { name: 'Branches', exact: true }).click();
  await page.getByRole('combobox', { name: 'Default branch', exact: true }).selectOption({ label: 'main' });
  await page.getByRole('button', { name: 'Update', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  await openRepository(page, FIXTURES.publicRepository);
  await expect(page.getByRole('button', { name: 'Branch main', exact: true })).toBeVisible();
});

// covers: REQ-4-3-3
test('REQ-4-3-3: the previous default branch still exists after the switch', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Branch main', exact: true }).click();
  await page.getByRole('textbox', { name: 'Find branch', exact: true }).fill('release');
  await expect(page.getByRole('option', { name: 'release', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Branch main', exact: true }).click();
  await page.getByRole('textbox', { name: 'Find branch', exact: true }).fill('main');
  await expect(page.getByRole('option', { name: 'main', exact: true })).toBeVisible();
});

// covers: REQ-4-3-3
test('REQ-4-3-3: a non-admin sees no default branch control', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  const settings = page.getByRole('link', { name: 'Settings', exact: true });
  if (await settings.count()) {
    await settings.click();
    const branches = page.getByRole('link', { name: 'Branches', exact: true });
    if (await branches.count()) {
      await branches.click();
    }
  }
  await expect(page.getByRole('combobox', { name: 'Default branch', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Update', exact: true })).toHaveCount(0);
});
