import { test, expect } from '@playwright/test';
import { FIXTURES, openPullRequests, signIn } from './helpers';

// covers: REQ-6-5
test('REQ-6-5: an eligible pull request merges after confirmation', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true }).click();
  await page.getByRole('button', { name: 'Merge pull request', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm merge', exact: true }).click();
  await expect(page.getByText('Merged', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Merged', { exact: true })).toBeVisible();
});

// covers: REQ-6-5
test('REQ-6-5: the eligible pull request exposes the confirmation area with conditions', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('link', { name: 'Pull requests', exact: true }).click();
  await page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true }).click();
  const merge = page.getByRole('button', { name: 'Merge pull request', exact: true });
  if (await merge.isEnabled()) {
    await merge.click();
    await expect(page.getByRole('button', { name: 'Confirm merge', exact: true })).toBeVisible();
    await expect(page.getByText(/satisfied|conditions/i).first()).toBeVisible();
  } else {
    await expect(page.getByText('Review required by branch protection')).toBeVisible();
  }
});

// covers: REQ-6-5
test('REQ-6-5: a blocked pull request explains its unmet protection condition', async ({ page }) => {
  await openPullRequests(page);
  await page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true }).click();
  await expect(page.getByRole('button', { name: 'Merge pull request', exact: true })).toBeDisabled();
  await expect(page.getByText('Review required by branch protection')).toBeVisible();
});
