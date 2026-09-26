import { test, expect } from '@playwright/test';
import { FIXTURES, openPullRequests, openRepository, signIn } from './helpers';

// covers: REQ-6-6
test('REQ-6-6: closing and reopening a pull request cycles without confirmation', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true }).click();
  await page.getByRole('button', { name: 'Close pull request', exact: true }).click();
  await expect(page.getByText('Closed', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Reopen pull request', exact: true }).click();
  await expect(page.getByText('Open', { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Close pull request', exact: true })).toBeVisible();
});

// covers: REQ-6-6
test('REQ-6-6: an unauthorized viewer gets neither close nor reopen controls', async ({ page }) => {
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true }).click();
  await expect(page.getByRole('heading', { name: FIXTURES.secondPullRequest, exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Close pull request', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Reopen pull request', exact: true })).toHaveCount(0);
});

// covers: REQ-6-6
test('REQ-6-6: a closed pull request keeps its discussion and diff viewable', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true }).click();
  await page.getByRole('button', { name: 'Close pull request', exact: true }).click();
  await expect(page.getByText('Closed', { exact: true }).first()).toBeVisible();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await expect(page.getByText('src/search.ts')).toBeVisible();
  await openPullRequests(page);
  await page.getByRole('link', { name: 'Closed', exact: true }).click();
  await expect(page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true })).toBeVisible();
});
