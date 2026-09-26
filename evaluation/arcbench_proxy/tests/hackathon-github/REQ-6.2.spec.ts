import { test, expect } from '@playwright/test';
import { FIXTURES, openPublicRepository } from './helpers';

// covers: REQ-6-2-1
test('REQ-6-2-1: filter public pull requests and restore the selected PR', async ({ page }) => {
  await openPublicRepository(page);
  await page.getByRole('link', { name: 'Pull requests', exact: true }).click();
  await page.getByRole('link', { name: 'Open', exact: true }).click();
  await expect(page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true })).toBeVisible();
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await expect(page.getByRole('heading', { name: FIXTURES.openPullRequest, exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: FIXTURES.openPullRequest, exact: true })).toBeVisible();
});
