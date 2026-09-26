import { expect, Page, test } from '@playwright/test';
import {
  commitNewFile,
  createBranch,
  createPullRequest,
  createRepository,
  openRepository,
  openRepositorySettings,
  signIn,
} from './helpers';

// Builds a dedicated repository with an open PR targeting its main branch, so
// the protection scenarios never depend on the seeded repository state.
async function setupProtectionScenario(page: Page): Promise<{ repository: string; branch: string }> {
  await signIn(page);
  const repository = 'pw-protection-' + String(Date.now());
  const branch = 'pw-protection-feature';
  await createRepository(page, repository, { readme: true });
  await openRepository(page, repository);
  await createBranch(page, branch, repository);
  await commitNewFile(page, 'protected-change.md', 'protection scenario change', 'Add protected change', branch);
  await createPullRequest(page, 'main', branch, 'Protection scenario PR', '', repository);
  return { repository, branch };
}

// covers: REQ-6-1
test('REQ-6-1: an admin creates a protection rule with both requirements', async ({ page }) => {
  const { repository } = await setupProtectionScenario(page);
  await openRepositorySettings(page, repository);
  await page.getByRole('link', { name: 'Branches', exact: true }).click();
  await page.getByRole('button', { name: 'Add branch protection rule', exact: true }).click();
  await page.getByLabel('Branch name pattern', { exact: true }).fill('main');
  await page.getByRole('checkbox', { name: 'Require 1 approval', exact: true }).check();
  await page.getByRole('checkbox', { name: 'Require status check test', exact: true }).check();
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await page.reload();
  await expect(page.getByText('main', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('1 approval')).toBeVisible();
  await expect(page.getByText('Require status check test')).toBeVisible();
});

// covers: REQ-6-1
test('REQ-6-1: a non-admin gets no rule creation button', async ({ page }) => {
  await openRepository(page, 'acme-docs');
  const settings = page.getByRole('link', { name: 'Settings', exact: true });
  if (await settings.count()) {
    await settings.click();
    const branches = page.getByRole('link', { name: 'Branches', exact: true });
    if (await branches.count()) {
      await branches.click();
    }
  }
  await expect(page.getByRole('button', { name: 'Add branch protection rule', exact: true })).toHaveCount(0);
});

// covers: REQ-6-1
test('REQ-6-1: an admin flips the test check from pending to success on the PR', async ({ page }) => {
  const { repository } = await setupProtectionScenario(page);
  await openRepository(page, repository);
  await page.getByRole('link', { name: 'Pull requests', exact: true }).click();
  await page.getByRole('link', { name: 'Protection scenario PR', exact: true }).click();
  const checks = page.getByText('test: pending', { exact: true });
  await expect(checks.first()).toBeVisible();
  await page.getByRole('combobox', { name: 'test status', exact: true }).selectOption({ label: 'success' });
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('test: success', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('test: success', { exact: true })).toBeVisible();
});
