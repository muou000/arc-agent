import { test, expect } from '@playwright/test';
import { createIssue, openRepository, signIn } from './helpers';

// covers: REQ-5-4
test('REQ-5-4: closing an issue records the transition without touching its content', async ({ page }) => {
  await signIn(page);
  const title = 'Closable issue ' + String(Date.now());
  await createIssue(page, title, 'close me');
  await page.getByRole('button', { name: 'Close issue', exact: true }).click();
  await expect(page.getByText('Closed', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Closed issue').first()).toBeVisible();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await expect(page.getByText('close me')).toBeVisible();
});

// covers: REQ-5-4
test('REQ-5-4: reopening restores the Open status and the close control', async ({ page }) => {
  await signIn(page);
  const title = 'Reopenable issue ' + String(Date.now());
  await createIssue(page, title, 'reopen me');
  await page.getByRole('button', { name: 'Close issue', exact: true }).click();
  await expect(page.getByText('Closed', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Reopen issue', exact: true }).click();
  await expect(page.getByText('Open', { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Close issue', exact: true })).toBeVisible();
});

// covers: REQ-5-4
test('REQ-5-4: a viewer without triage rights sees no close or reopen controls', async ({ page }) => {
  await openRepository(page, 'acme-docs');
  await page.getByRole('link', { name: 'Issues', exact: true }).click();
  await page.getByRole('link', { name: 'Improve onboarding', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Improve onboarding', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Close issue', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Reopen issue', exact: true })).toHaveCount(0);
});
