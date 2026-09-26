import { test, expect } from '@playwright/test';
import { FIXTURES, openRepository, signIn } from './helpers';

// covers: REQ-3-4
test('REQ-3-4: an admin makes a private repository public without retyping the name', async ({ page }) => {
  await signIn(page);
  await openRepository(page, FIXTURES.privateRepository);
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('link', { name: 'General', exact: true }).click();
  await page.getByRole('button', { name: 'Change visibility', exact: true }).click();
  await page.getByRole('radio', { name: 'Public', exact: true }).check();
  await page.getByRole('button', { name: 'Confirm visibility', exact: true }).click();
  await expect(page.getByText('Public', { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText('Public', { exact: true }).first()).toBeVisible();
});

// covers: REQ-3-4
// Must run after the visibility change test in this file: it reads the same
// repository through a fresh unauthenticated session.
test('REQ-3-4: a visitor opening the newly public address sees the repository', async ({ page }) => {
  await openRepository(page, FIXTURES.privateRepository);
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.privateRepository, 'i') })).toBeVisible();
  await expect(page.getByText('Public', { exact: true }).first()).toBeVisible();
});

// covers: REQ-3-4
test('REQ-3-4: a non-admin viewer gets no visibility control', async ({ page }) => {
  await openRepository(page, FIXTURES.publicRepository);
  const settings = page.getByRole('link', { name: 'Settings', exact: true });
  if (await settings.count()) {
    await settings.click();
    const general = page.getByRole('link', { name: 'General', exact: true });
    if (await general.count()) {
      await general.click();
    }
  }
  await expect(page.getByRole('button', { name: 'Change visibility', exact: true })).toHaveCount(0);
});
