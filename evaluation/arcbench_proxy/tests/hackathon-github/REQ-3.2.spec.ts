import { test, expect } from '@playwright/test';
import { FIXTURES, createRepository, openRepository, signIn } from './helpers';

// covers: REQ-3-2-1
test('REQ-3-2-1: a duplicate repository name stays on the form', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: 'New repository', exact: true }).click();
  await page.getByLabel('Repository name', { exact: true }).fill(FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Create repository', exact: true }).click();
  await expect(page.getByLabel('Repository name', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toHaveCount(0);
});

// covers: REQ-3-2-1
test('REQ-3-2-1: an empty repository name is rejected without navigation', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: 'New repository', exact: true }).click();
  await page.getByRole('button', { name: 'Create repository', exact: true }).click();
  await expect(page.getByLabel('Repository name', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: /new repository/i }).first()).toBeVisible();
});

// covers: REQ-3-2-1
test('REQ-3-2-1: a public repository keeps its description and visibility marker', async ({ page }) => {
  const name = 'pw-public-' + String(Date.now());
  await createRepository(page, name, {
    description: 'Repository created by Playwright',
    visibility: 'Public',
    readme: true,
  });
  await expect(page.getByText('Public', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Repository created by Playwright')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(name, 'i') })).toBeVisible();
  await expect(page.getByText('Repository created by Playwright')).toBeVisible();
});

// covers: REQ-3-2-2
test('REQ-3-2-2: forking shows the source relationship and survives reload', async ({ page }) => {
  await signIn(page);
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Fork', exact: true }).click();
  const forkName = 'pw-fork-' + String(Date.now());
  await page.getByLabel('Repository name', { exact: true }).fill(forkName);
  await page.getByRole('button', { name: 'Create fork', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(forkName, 'i') })).toBeVisible();
  await expect(page.getByText(new RegExp('Forked from .+' ))).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(forkName, 'i') })).toBeVisible();
  await expect(page.getByText(new RegExp('Forked from .+'))).toBeVisible();
});

// covers: REQ-3-2-2
test('REQ-3-2-2: an existing fork name in the namespace prevents creation', async ({ page }) => {
  await signIn(page);
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Fork', exact: true }).click();
  await page.getByLabel('Repository name', { exact: true }).fill('acme-docs-fork');
  await page.getByRole('button', { name: 'Create fork', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp('acme-docs-fork', 'i') })).toHaveCount(0);
  await expect(page.getByLabel('Repository name', { exact: true })).toBeVisible();
});

// covers: REQ-3-2-2
test('REQ-3-2-2: forking a private source keeps the fork private', async ({ page }) => {
  await signIn(page);
  await openRepository(page, FIXTURES.privateRepository);
  await page.getByRole('button', { name: 'Fork', exact: true }).click();
  const forkName = 'pw-private-fork-' + String(Date.now());
  await page.getByLabel('Repository name', { exact: true }).fill(forkName);
  await page.getByRole('button', { name: 'Create fork', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(forkName, 'i') })).toBeVisible();
  await expect(page.getByText('Private', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(new RegExp('Forked from .+'))).toBeVisible();
});

// covers: REQ-3-2-3
test('REQ-3-2-3: the HTTPS clone value is copied to the clipboard', async ({ page }) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Code', exact: true }).click();
  await page.getByRole('tab', { name: 'HTTPS', exact: true }).click();
  await page.getByRole('button', { name: 'Copy clone value', exact: true }).click();
  await expect(page.getByText('Copied', { exact: true })).toBeVisible();
  const value = await page.evaluate(() => navigator.clipboard.readText());
  expect(value).toContain('https://');
  expect(value).toContain(FIXTURES.publicRepository);
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
});

// covers: REQ-3-2-3
test('REQ-3-2-3: the SSH clone value uses the SSH format with the .git suffix', async ({ page }) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Code', exact: true }).click();
  await page.getByRole('tab', { name: 'SSH', exact: true }).click();
  await page.getByRole('button', { name: 'Copy clone value', exact: true }).click();
  await expect(page.getByText('Copied', { exact: true })).toBeVisible();
  const value = await page.evaluate(() => navigator.clipboard.readText());
  expect(value).toMatch(/^git@.+:.+\.git$/);
  expect(value).toContain(FIXTURES.publicRepository);
});

// covers: REQ-3-2-3
test('REQ-3-2-3: copying leaves the repository untouched and the menu reusable', async ({ page }) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Code', exact: true }).click();
  await page.getByRole('button', { name: 'Copy clone value', exact: true }).click();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
  await page.getByRole('button', { name: 'Code', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Copy clone value', exact: true })).toBeVisible();
  await expect(page.getByText('Public', { exact: true }).first()).toBeVisible();
});
