import { Page, test, expect } from '@playwright/test';
import {
  FIXTURES,
  openPullRequests,
  provisionReviewer,
  signIn,
  signInAs,
} from './helpers';

// covers: REQ-6-3-1
test('REQ-6-3-1: a visitor opens the public pull request without signing in', async ({ page }) => {
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await expect(page.getByRole('heading', { name: FIXTURES.openPullRequest, exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: FIXTURES.openPullRequest, exact: true })).toBeVisible();
});

// covers: REQ-6-3-1
test('REQ-6-3-1: the Commits tab reports the comparable commit summary', async ({ page }) => {
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Commits', exact: true }).click();
  await expect(page.getByText(/Commit summary|commits?/i).first()).toBeVisible();
});

// covers: REQ-6-3-1
test('REQ-6-3-1: the Files changed entry reports the changed files summary', async ({ page }) => {
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await expect(page.getByText(/Changed files/).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText(/Changed files/).first()).toBeVisible();
});

// covers: REQ-6-3-2
test('REQ-6-3-2: the diff shows the known path with aggregate line counts', async ({ page }) => {
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await expect(page.getByText('src/search.ts')).toBeVisible();
  await expect(page.getByText(/3 additions/)).toBeVisible();
  await expect(page.getByText(/1 deletions?/)).toBeVisible();
});

// covers: REQ-6-3-2
test('REQ-6-3-2: the diff blocks persist after reload without modification', async ({ page }) => {
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await expect(page.getByText('src/search.ts')).toBeVisible();
  await page.reload();
  await expect(page.getByText('src/search.ts')).toBeVisible();
  await expect(page.getByText(/3 additions/)).toBeVisible();
});

// covers: REQ-6-3-3
test('REQ-6-3-3: a single review comment publishes on the changed line', async ({ page }) => {
  const reviewer = 'pw-line-reviewer-' + String(Date.now());
  await signIn(page);
  await provisionReviewer(page, reviewer);
  await signInAsReviewer(page, reviewer);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await page.getByRole('button', { name: 'Add comment', exact: true }).first().click();
  const body = 'Inline note ' + String(Date.now());
  await page.getByLabel('Comment', { exact: true }).last().fill(body);
  await page.getByRole('button', { name: 'Add single comment', exact: true }).click();
  await expect(page.getByText(body)).toBeVisible();
  await page.reload();
  await expect(page.getByText(body)).toBeVisible();
});

// covers: REQ-6-3-3
test('REQ-6-3-3: starting a review keeps the comment as a pending draft', async ({ page }) => {
  const reviewer = 'pw-draft-reviewer-' + String(Date.now());
  await signIn(page);
  await provisionReviewer(page, reviewer);
  await signInAsReviewer(page, reviewer);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await page.getByRole('button', { name: 'Add comment', exact: true }).first().click();
  const body = 'Pending note ' + String(Date.now());
  await page.getByLabel('Comment', { exact: true }).last().fill(body);
  await page.getByRole('button', { name: 'Start a review', exact: true }).click();
  await expect(page.getByText(body)).toBeVisible();
  await expect(page.getByText('Pending review')).toBeVisible();
  await page.reload();
  await expect(page.getByText('Pending review')).toBeVisible();
});

// covers: REQ-6-3-4
test('REQ-6-3-4: an approve review without a summary is accepted', async ({ page }) => {
  const reviewer = 'pw-approver-' + String(Date.now());
  await signIn(page);
  await provisionReviewer(page, reviewer);
  await signInAsReviewer(page, reviewer);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  await page.getByRole('radio', { name: 'Approve', exact: true }).check();
  await page.getByRole('button', { name: 'Submit review', exact: true }).click();
  await expect(page.getByText('Approved', { exact: true })).toBeVisible();
});

// covers: REQ-6-3-4
test('REQ-6-3-4: a request-changes review shows its summary and persists', async ({ page }) => {
  const reviewer = 'pw-requester-' + String(Date.now());
  await signIn(page);
  await provisionReviewer(page, reviewer);
  await signInAsReviewer(page, reviewer);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  await page.getByRole('radio', { name: 'Request changes', exact: true }).check();
  const summary = 'Please address the boundary case ' + String(Date.now());
  await page.getByLabel('Summary', { exact: true }).fill(summary);
  await page.getByRole('button', { name: 'Submit review', exact: true }).click();
  await expect(page.getByText('Changes requested')).toBeVisible();
  await expect(page.getByText(summary)).toBeVisible();
  await page.reload();
  await expect(page.getByText('Changes requested')).toBeVisible();
  await expect(page.getByText(summary)).toBeVisible();
});

// covers: REQ-6-3-4
test('REQ-6-3-4: a new decision by the same reviewer replaces the previous one', async ({ page }) => {
  const reviewer = 'pw-replace-' + String(Date.now());
  await signIn(page);
  await provisionReviewer(page, reviewer);
  await signInAsReviewer(page, reviewer);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true }).click();
  await page.getByRole('link', { name: 'Files changed', exact: true }).click();
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  await page.getByRole('radio', { name: 'Request changes', exact: true }).check();
  await page.getByRole('button', { name: 'Submit review', exact: true }).click();
  await expect(page.getByText('Changes requested')).toBeVisible();
  await page.getByRole('button', { name: 'Review changes', exact: true }).click();
  await page.getByRole('radio', { name: 'Approve', exact: true }).check();
  await page.getByRole('button', { name: 'Submit review', exact: true }).click();
  await expect(page.getByText('Approved', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Approved', { exact: true })).toBeVisible();
});

async function signInAsReviewer(page: Page, reviewer: string) {
  await signInAs(page, reviewer, 'Reviewer-password-123!');
}
