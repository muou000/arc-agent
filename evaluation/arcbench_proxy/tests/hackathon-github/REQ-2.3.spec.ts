import { test, expect } from '@playwright/test';
import { FIXTURES, grantRepositoryAccess, openRepositorySettings, signIn } from './helpers';

// covers: REQ-2-3
test('REQ-2-3: grant a team Write access from Manage access', async ({ page }) => {
  await signIn(page);
  await openRepositorySettings(page);
  await page.getByRole('link', { name: 'Manage access', exact: true }).click();
  await page.getByRole('button', { name: 'Add people or teams', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search', exact: true }).fill(FIXTURES.teamName);
  await page.getByRole('option', { name: new RegExp(FIXTURES.teamName) }).first().click();
  await page.getByRole('combobox', { name: 'Role', exact: true }).selectOption({ label: 'Write' });
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  const row = page.getByRole('listitem').or(page.locator('li')).filter({ hasText: FIXTURES.teamName }).first();
  await expect(page.getByText(FIXTURES.teamName, { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Write', { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText(FIXTURES.teamName, { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Write', { exact: true }).first()).toBeVisible();
});

// covers: REQ-2-3
test('REQ-2-3: replacing a role keeps exactly one row for the subject', async ({ page }) => {
  await signIn(page);
  await grantRepositoryAccess(page, FIXTURES.teamName, 'Write');
  const row = page.locator('li, tr').filter({ hasText: FIXTURES.teamName }).first();
  await row.getByRole('combobox', { name: 'Role', exact: true }).selectOption({ label: 'Read' });
  await row.getByRole('button', { name: 'Save', exact: true }).click();
  await page.reload();
  const rows = page.locator('li, tr').filter({ hasText: FIXTURES.teamName });
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText('Read');
  await expect(rows.first()).not.toContainText('Write');
});

// covers: REQ-2-3
test('REQ-2-3: saving the same role again does not duplicate the grant', async ({ page }) => {
  await signIn(page);
  await grantRepositoryAccess(page, FIXTURES.teamName, 'Read');
  await openRepositorySettings(page);
  await page.getByRole('link', { name: 'Manage access', exact: true }).click();
  const before = await page.locator('li, tr').filter({ hasText: FIXTURES.teamName }).count();
  await page.getByRole('button', { name: 'Add people or teams', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search', exact: true }).fill(FIXTURES.teamName);
  await page.getByRole('option', { name: new RegExp(FIXTURES.teamName) }).first().click();
  await page.getByRole('combobox', { name: 'Role', exact: true }).selectOption({ label: 'Read' });
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  await page.reload();
  const after = await page.locator('li, tr').filter({ hasText: FIXTURES.teamName }).count();
  expect(after).toBe(Math.max(before, 1));
});
