import { test, expect } from '@playwright/test';
import { FIXTURES, openHome } from './helpers';

// covers: REQ-1-1-1 REQ-1-1-2
test('REQ-1-1-1 and REQ-1-1-2: register and sign in with a new account', async ({ page }) => {
  const suffix = String(Date.now());
  const username = 'proxy-user-' + suffix;
  const email = username + '@example.test';
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByRole('link', { name: 'Create an account', exact: true }).click();
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Email', { exact: true }).fill(email);
  await page.getByLabel('Password', { exact: true }).fill(FIXTURES.password);
  await page.getByLabel('Confirm password', { exact: true }).fill(FIXTURES.password);
  await page.getByRole('checkbox', { name: 'Agree to the terms', exact: true }).check();
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByLabel('Username or email', { exact: true })).toBeVisible();
  await page.getByLabel('Username or email', { exact: true }).fill(email);
  await page.getByLabel('Password', { exact: true }).fill(FIXTURES.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});
