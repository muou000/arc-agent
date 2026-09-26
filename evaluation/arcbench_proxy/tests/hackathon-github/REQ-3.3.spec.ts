import { test, expect } from '@playwright/test';
import { FIXTURES, openRepository } from './helpers';

// covers: REQ-3-3
test('REQ-3-3: a visitor reads the public overview with its visibility marker', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
  await expect(page.getByText('Public', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
  await expect(page.getByText('Public', { exact: true })).toBeVisible();
});

// covers: REQ-3-3
test('REQ-3-3: the Code navigation link is distinct from the clone Code button', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await expect(page.getByRole('link', { name: 'Code', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Code', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Code', exact: true })).toBeVisible();
});

// covers: REQ-3-3
test('REQ-3-3: the overview presents collaboration entry points for browsing', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await expect(page.getByRole('link', { name: 'Issues', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Pull requests', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Commits', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('link', { name: 'Issues', exact: true })).toBeVisible();
});
