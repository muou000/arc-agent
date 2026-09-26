import { test, expect } from '@playwright/test';
import { FIXTURES, openHome, openRepository } from './helpers';

// covers: REQ-3-1
test('REQ-3-1: searching for a repository opens it directly and survives reload', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
});

// covers: REQ-3-1
test('REQ-3-1: a query without matches shows No results consistently', async ({ page }) => {
  await openHome(page);
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill('no-such-repository-xyz');
  await search.press('Enter');
  await expect(page.getByText('No results', { exact: true })).toBeVisible();
  await openHome(page);
  const repeat = page.getByRole('searchbox', { name: 'Search', exact: true });
  await repeat.fill('no-such-repository-xyz');
  await repeat.press('Enter');
  await expect(page.getByText('No results', { exact: true })).toBeVisible();
});

// covers: REQ-3-1
test('REQ-3-1: a private repository name exposes no result link to a visitor', async ({ page }) => {
  await openHome(page);
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill(FIXTURES.privateRepository);
  await search.press('Enter');
  await expect(page.getByRole('link', { name: FIXTURES.privateRepository, exact: true })).toHaveCount(0);
});
