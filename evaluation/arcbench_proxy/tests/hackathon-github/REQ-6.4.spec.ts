import { test, expect } from '@playwright/test';
import { FIXTURES, openPullRequests, signIn } from './helpers';

// covers: REQ-6-4
test('REQ-6-4: requesting a reviewer saves immediately and persists', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('button', { name: 'Reviewers', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search', exact: true }).fill(FIXTURES.member);
  await page.getByRole('option', { name: FIXTURES.member, exact: true }).click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toBeVisible();
});

// covers: REQ-6-4
test('REQ-6-4: removing a reviewer request needs no confirmation step', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('button', { name: 'Reviewers', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search', exact: true }).fill(FIXTURES.member);
  await page.getByRole('option', { name: FIXTURES.member, exact: true }).click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: `Remove ${FIXTURES.member}`, exact: true }).click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toHaveCount(0);
});

// covers: REQ-6-4
test('REQ-6-4: the pull request author is not offered as a reviewer candidate', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('button', { name: 'Reviewers', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search', exact: true }).fill(FIXTURES.username);
  await expect(page.getByRole('option', { name: FIXTURES.username, exact: true })).toHaveCount(0);
});
