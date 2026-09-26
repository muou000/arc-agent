import { expect, Page } from '@playwright/test';

export const FIXTURES = {
  username: 'alice-dev',
  email: 'alice.dev@example.test',
  password: 'Valid-password-123!',
  organization: 'Acme Demo',
  publicRepository: 'acme-docs',
  privateRepository: 'secret-research',
  openIssue: 'Improve onboarding',
  openPullRequest: 'Improve onboarding',
} as const;

export async function openHome(page: Page): Promise<void> {
  await page.goto('/');
}

export async function signIn(page: Page): Promise<void> {
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(FIXTURES.username);
  await page.getByLabel('Password', { exact: true }).fill(FIXTURES.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
}

export async function openPublicRepository(page: Page): Promise<void> {
  await openHome(page);
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill(FIXTURES.publicRepository);
  await search.press('Enter');
  await page.getByRole('link', { name: FIXTURES.publicRepository, exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
}
