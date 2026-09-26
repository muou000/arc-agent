import { test, expect } from '@playwright/test';
import { FIXTURES, openRepository } from './helpers';

// covers: REQ-4-2-1
test('REQ-4-2-1: branch history shows the seeded commit, author and relative time', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'Commits', exact: true }).click();
  await expect(page.getByText('Document search flow')).toBeVisible();
  await expect(page.getByText(new RegExp(FIXTURES.username))).toBeVisible();
  await expect(page.getByText(/ago/).first()).toBeVisible();
});

// covers: REQ-4-2-1
test('REQ-4-2-1: a file page exposes its own Commits history link', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'README.md', exact: true }).click();
  await expect(page.getByText('Document search flow')).toBeVisible();
  await page.getByRole('link', { name: 'Commits', exact: true }).click();
  await expect(page.getByText('Document search flow')).toBeVisible();
});

// covers: REQ-4-2-1
test('REQ-4-2-1: reloading the history page keeps the commit entries', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'Commits', exact: true }).click();
  await expect(page.getByText('Document search flow')).toBeVisible();
  await page.reload();
  await expect(page.getByText('Document search flow')).toBeVisible();
  await expect(page.getByText(/ago/).first()).toBeVisible();
});

// covers: REQ-4-2-2
test('REQ-4-2-2: opening a commit entry lists its changed file path verbatim', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'Commits', exact: true }).click();
  await page.getByRole('link', { name: /Document search flow/ }).first().click();
  await expect(page.getByText('src/search.ts')).toBeVisible();
  await expect(page.getByText('Changed files')).toBeVisible();
});

// covers: REQ-4-2-2
test('REQ-4-2-2: the diff page reports numeric addition and deletion counts', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'Commits', exact: true }).click();
  await page.getByRole('link', { name: /Document search flow/ }).first().click();
  await expect(page.getByText(/\d+ additions?/)).toBeVisible();
  await expect(page.getByText(/\d+ deletions?/)).toBeVisible();
});

// covers: REQ-4-2-2
test('REQ-4-2-2: the commit diff is readable without prior navigation and persists', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'Commits', exact: true }).click();
  await page.getByRole('link', { name: /Document search flow/ }).first().click();
  await expect(page.getByText('src/search.ts')).toBeVisible();
  await page.reload();
  await expect(page.getByText('src/search.ts')).toBeVisible();
  await expect(page.getByText('Changed files')).toBeVisible();
});

// covers: REQ-4-2-3
test('REQ-4-2-3: a code search opens the matching file from its result link', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill('search flow');
  await search.press('Enter');
  await page.getByRole('link', { name: 'Code', exact: true }).click();
  await page.getByRole('link', { name: 'README.md', exact: true }).first().click();
  await expect(page.getByText('Document search flow')).toBeVisible();
  await page.reload();
  await expect(page.getByText('Document search flow')).toBeVisible();
  await expect(page.getByRole('link', { name: 'README.md', exact: true }).first()).toBeVisible();
});

// covers: REQ-4-2-3
test('REQ-4-2-3: an absent query shows No code results and keeps the query', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill('no-such-token');
  await search.press('Enter');
  await page.getByRole('link', { name: 'Code', exact: true }).click();
  await expect(page.getByText('No code results')).toBeVisible();
  await expect(page.getByRole('searchbox', { name: 'Search', exact: true })).toHaveValue('no-such-token');
});

// covers: REQ-4-2-3
test('REQ-4-2-3: repeating the same empty search produces no stale matches', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill('no-such-token');
  await search.press('Enter');
  await page.getByRole('link', { name: 'Code', exact: true }).click();
  await expect(page.getByText('No code results')).toBeVisible();
  await openRepository(page, FIXTURES.publicRepository);
  const repeat = page.getByRole('searchbox', { name: 'Search', exact: true });
  await repeat.fill('no-such-token');
  await repeat.press('Enter');
  await page.getByRole('link', { name: 'Code', exact: true }).click();
  await expect(page.getByText('No code results')).toBeVisible();
  await expect(page.getByRole('link', { name: 'README.md', exact: true })).toHaveCount(0);
});
