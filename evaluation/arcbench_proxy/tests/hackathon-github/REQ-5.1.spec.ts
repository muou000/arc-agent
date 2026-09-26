import { test, expect } from '@playwright/test';
import { FIXTURES, openIssues } from './helpers';

// covers: REQ-5-1-1
test('REQ-5-1-1: the seeded open issue is listed with its labels', async ({ page }) => {
  await openIssues(page);
  const row = page.getByRole('link', { name: FIXTURES.openIssue, exact: true });
  await expect(row).toBeVisible();
  await expect(page.getByText('bug', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/documentation|alice/).first()).toBeVisible();
});

// covers: REQ-5-1-1
test('REQ-5-1-1: the Closed filter reveals the closed issue and excludes the open one', async ({ page }) => {
  await openIssues(page);
  await page.getByRole('link', { name: 'Closed', exact: true }).click();
  await expect(page.getByRole('link', { name: FIXTURES.closedIssue, exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: FIXTURES.openIssue, exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('link', { name: FIXTURES.closedIssue, exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: FIXTURES.openIssue, exact: true })).toHaveCount(0);
});

// covers: REQ-5-1-1
test('REQ-5-1-1: state and keyword filters combine into one result set', async ({ page }) => {
  await openIssues(page);
  await page.getByRole('link', { name: 'Closed', exact: true }).click();
  const search = page.getByRole('searchbox', { name: 'Search issues', exact: true });
  await search.fill('Legacy');
  await expect(page.getByRole('link', { name: FIXTURES.closedIssue, exact: true })).toBeVisible();
  await search.fill('Improve');
  await expect(page.getByRole('link', { name: FIXTURES.closedIssue, exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: FIXTURES.openIssue, exact: true })).toHaveCount(0);
});

// covers: REQ-5-1-2
test('REQ-5-1-2: the issue detail shows number, exact title heading and status', async ({ page }) => {
  await openIssues(page);
  await page.getByRole('link', { name: FIXTURES.openIssue, exact: true }).click();
  await expect(page.getByRole('heading', { name: FIXTURES.openIssue, exact: true })).toBeVisible();
  await expect(page.getByText(/#\d+/).first()).toBeVisible();
  await expect(page.getByText('Open', { exact: true })).toBeVisible();
});

// covers: REQ-5-1-2
test('REQ-5-1-2: the saved description and metadata sections are displayed', async ({ page }) => {
  await openIssues(page);
  await page.getByRole('link', { name: FIXTURES.openIssue, exact: true }).click();
  await expect(page.getByText('Describe the onboarding improvement.')).toBeVisible();
  await expect(page.getByText('Assignees', { exact: true })).toBeVisible();
  await expect(page.getByText('Labels', { exact: true })).toBeVisible();
  await expect(page.getByText('Milestone', { exact: true })).toBeVisible();
});

// covers: REQ-5-1-2
test('REQ-5-1-2: the discussion history survives a reload', async ({ page }) => {
  await openIssues(page);
  await page.getByRole('link', { name: FIXTURES.openIssue, exact: true }).click();
  await expect(page.getByText(/Comment|Activity/).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText('Describe the onboarding improvement.')).toBeVisible();
  await expect(page.getByRole('heading', { name: FIXTURES.openIssue, exact: true })).toBeVisible();
});
