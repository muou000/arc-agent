import { test, expect } from '@playwright/test';
import { FIXTURES, openPublicRepository } from './helpers';

// covers: REQ-5-1-1
test('REQ-5-1-1: filter repository issues by status and title', async ({ page }) => {
  await openPublicRepository(page);
  await page.getByRole('link', { name: 'Issues', exact: true }).click();
  await page.getByRole('link', { name: 'Open', exact: true }).click();
  await page.getByRole('searchbox', { name: 'Search issues', exact: true }).fill('onboarding');
  await expect(page.getByRole('link', { name: FIXTURES.openIssue, exact: true })).toBeVisible();
  await page.getByRole('link', { name: FIXTURES.openIssue, exact: true }).click();
  await expect(page.getByRole('heading', { name: FIXTURES.openIssue, exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: FIXTURES.openIssue, exact: true })).toBeVisible();
});
