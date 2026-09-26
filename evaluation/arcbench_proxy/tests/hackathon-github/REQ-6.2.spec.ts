import { test, expect } from '@playwright/test';
import {
  commitNewFile,
  createBranch,
  FIXTURES,
  openPullRequests,
  openRepository,
  signIn,
} from './helpers';

// covers: REQ-6-2-1
test('REQ-6-2-1: the Closed filter excludes the open pull request', async ({ page }) => {
  await openPullRequests(page);
  await page.getByRole('link', { name: 'Closed', exact: true }).click();
  await expect(page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: 'Open', exact: true }).click();
  await expect(page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true })).toBeVisible();
});

// covers: REQ-6-2-1
test('REQ-6-2-1: the seeded list carries both pull request titles', async ({ page }) => {
  await openPullRequests(page);
  await expect(page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: FIXTURES.secondPullRequest, exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('link', { name: FIXTURES.openPullRequest, exact: true })).toBeVisible();
});

// covers: REQ-6-2-2
test('REQ-6-2-2: comparing main with feature-search reports the changed file', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: 'New pull request', exact: true }).click();
  await page.getByRole('combobox', { name: 'base', exact: true }).selectOption({ label: 'main' });
  await page.getByRole('combobox', { name: 'compare', exact: true }).selectOption({ label: 'feature-search' });
  await page.getByRole('button', { name: 'Compare changes', exact: true }).click();
  await expect(page.getByText('src/search.ts')).toBeVisible();
  await expect(page.getByText(/Commit summary|commits?/i).first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create pull request', exact: true })).toBeEnabled();
});

// covers: REQ-6-2-2
test('REQ-6-2-2: identical branches show No changes and disable creation at once', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: 'New pull request', exact: true }).click();
  await page.getByRole('combobox', { name: 'base', exact: true }).selectOption({ label: 'main' });
  await page.getByRole('combobox', { name: 'compare', exact: true }).selectOption({ label: 'main' });
  await expect(page.getByText('No changes')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create pull request', exact: true })).toBeDisabled();
});

// covers: REQ-6-2-2
test('REQ-6-2-2: a visitor gets no pull request creation entry', async ({ page }) => {
  await openPullRequests(page);
  await expect(page.getByRole('link', { name: 'New pull request', exact: true })).toHaveCount(0);
});

// covers: REQ-6-2-3
test('REQ-6-2-3: a blank title is rejected without creating a pull request', async ({ page }) => {
  await signIn(page);
  const compare = 'pw-reject-' + String(Date.now());
  await openRepository(page, FIXTURES.publicRepository);
  await createBranch(page, compare);
  await commitNewFile(page, 'reject-change.md', 'reject scenario', 'Add reject change', compare);
  await openPullRequests(page);
  await page.getByRole('link', { name: 'New pull request', exact: true }).click();
  await page.getByRole('combobox', { name: 'base', exact: true }).selectOption({ label: 'main' });
  await page.getByRole('combobox', { name: 'compare', exact: true }).selectOption({ label: compare });
  await page.getByRole('button', { name: 'Compare changes', exact: true }).click();
  await page.getByLabel('Title', { exact: true }).fill('   ');
  await page.getByRole('button', { name: 'Create pull request', exact: true }).click();
  await expect(page.getByText('Title is required')).toBeVisible();
  await openPullRequests(page);
  await page.getByRole('link', { name: 'Open', exact: true }).click();
  await expect(page.getByRole('link', { name: '   ', exact: true })).toHaveCount(0);
});

// covers: REQ-6-2-3
test('REQ-6-2-3: creating a pull request opens its detail with Open status', async ({ page }) => {
  await signIn(page);
  const compare = 'pw-pr-' + String(Date.now());
  await openRepository(page, FIXTURES.publicRepository);
  await createBranch(page, compare);
  await commitNewFile(page, 'pr-change.md', 'pull request scenario', 'Add pull request change', compare);
  const title = 'Proxy PR ' + String(Date.now());
  await openPullRequests(page);
  await page.getByRole('link', { name: 'New pull request', exact: true }).click();
  await page.getByRole('combobox', { name: 'base', exact: true }).selectOption({ label: 'main' });
  await page.getByRole('combobox', { name: 'compare', exact: true }).selectOption({ label: compare });
  await page.getByRole('button', { name: 'Compare changes', exact: true }).click();
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByLabel('Description', { exact: true }).fill('Created from the comparison page.');
  await page.getByRole('button', { name: 'Create pull request', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await expect(page.getByText('Open', { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await openPullRequests(page);
  await expect(page.getByRole('link', { name: title, exact: true })).toBeVisible();
});

// covers: REQ-6-2-4
test('REQ-6-2-4: a draft pull request shows its marker and a disabled merge button', async ({ page }) => {
  await signIn(page);
  const compare = 'pw-draft-' + String(Date.now());
  await openRepository(page, FIXTURES.publicRepository);
  await createBranch(page, compare);
  await commitNewFile(page, 'draft-change.md', 'draft scenario', 'Add draft change', compare);
  const title = 'Proxy draft PR ' + String(Date.now());
  await openPullRequests(page);
  await page.getByRole('link', { name: 'New pull request', exact: true }).click();
  await page.getByRole('combobox', { name: 'base', exact: true }).selectOption({ label: 'main' });
  await page.getByRole('combobox', { name: 'compare', exact: true }).selectOption({ label: compare });
  await page.getByRole('button', { name: 'Compare changes', exact: true }).click();
  await page.getByRole('button', { name: 'Create draft pull request', exact: true }).click();
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByRole('button', { name: 'Create draft pull request', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await expect(page.getByText('Draft', { exact: true }).first()).toBeVisible();
  const merge = page.getByRole('button', { name: 'Merge pull request', exact: true });
  await expect(merge).toBeDisabled();
});

// covers: REQ-6-2-4
test('REQ-6-2-4: the seeded draft becomes ready for review', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: 'Draft onboarding update', exact: true }).click();
  await expect(page.getByText('Draft', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Ready for review', exact: true }).click();
  await expect(page.getByText('Open', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Draft', { exact: true })).toHaveCount(0);
});

// covers: REQ-6-2-4
test('REQ-6-2-4: the ready-for-review transition preserves title, branches and state', async ({ page }) => {
  await signIn(page);
  await openPullRequests(page);
  await page.getByRole('link', { name: 'Draft onboarding update', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Draft onboarding update', exact: true })).toBeVisible();
  await expect(page.getByText('draft-feature')).toBeVisible();
  await expect(page.getByText('main', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Ready for review', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Draft onboarding update', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Open', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('draft-feature')).toBeVisible();
});
