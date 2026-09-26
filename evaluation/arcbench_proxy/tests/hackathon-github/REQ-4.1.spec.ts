import { test, expect } from '@playwright/test';
import { FIXTURES, openRepository } from './helpers';

// covers: REQ-4-1
test('REQ-4-1: a visitor opens the seeded readme and reads its content', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'README.md', exact: true }).click();
  await expect(page.getByText('Document search flow')).toBeVisible();
  await page.reload();
  await expect(page.getByText('Document search flow')).toBeVisible();
});

// covers: REQ-4-1
test('REQ-4-1: directory entries open nested files by their exact names', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  const docs = page.getByRole('link', { name: 'docs', exact: true });
  await expect(docs).toBeVisible();
  await docs.click();
  await page.getByRole('link', { name: 'guide.md', exact: true }).click();
  await expect(page.getByText(/guide/i).first()).toBeVisible();
  await page.reload();
  await expect(page.getByRole('link', { name: 'guide.md', exact: true }).or(page.getByText(/guide/i).first())).toBeVisible();
});

// covers: REQ-4-1
test('REQ-4-1: a directory page shows its current path', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'docs', exact: true }).click();
  await expect(page.getByText(/docs/).first()).toBeVisible();
  await expect(page.getByRole('link', { name: 'guide.md', exact: true })).toBeVisible();
});
