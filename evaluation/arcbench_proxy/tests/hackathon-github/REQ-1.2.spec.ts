import { test, expect } from '@playwright/test';
import { FIXTURES, openHome, openOrganization, registerAccount, signInAs, signOut } from './helpers';

// covers: REQ-1-2
test('REQ-1-2: cancelling the sign-out dialog keeps the session alive', async ({ page }) => {
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Sign out', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Sign out', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});

// covers: REQ-1-2
test('REQ-1-2: confirming sign out restores the unauthenticated state everywhere', async ({ page }) => {
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Sign out', exact: true }).click();
  await page.getByRole('dialog', { name: 'Sign out', exact: true }).getByRole('button', { name: 'Confirm sign out', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible();
  await openOrganization(page);
  await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toHaveCount(0);
});

// covers: REQ-1-2
test('REQ-1-2: browser back after sign out does not resurrect the session', async ({ page }) => {
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await openOrganization(page);
  await page.goBack();
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Sign out', exact: true }).click();
  await page.getByRole('dialog', { name: 'Sign out', exact: true }).getByRole('button', { name: 'Confirm sign out', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible();
});

// covers: REQ-1-3
test('REQ-1-3: update the password and sign in with the new credential', async ({ page }) => {
  const suffix = String(Date.now());
  const username = 'pw-pwd-' + suffix;
  const email = `${username}@example.test`;
  await registerAccount(page, username, email, FIXTURES.password);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('link', { name: 'Password and authentication', exact: true }).click();
  await page.getByLabel('Current password', { exact: true }).fill(FIXTURES.password);
  await page.getByLabel('New password', { exact: true }).fill('New-password-456!');
  await page.getByLabel('Confirm password', { exact: true }).fill('New-password-456!');
  await page.getByRole('button', { name: 'Update password', exact: true }).click();
  await expect(page.getByText('Password updated', { exact: true })).toBeVisible();
  await signOut(page);
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(email);
  await page.getByLabel('Password', { exact: true }).fill(FIXTURES.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByText('Invalid credentials', { exact: true })).toBeVisible();
  await signInAs(page, email, 'New-password-456!');
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});

// covers: REQ-1-3
test('REQ-1-3: an empty current password is rejected without changing credentials', async ({ page }) => {
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('link', { name: 'Password and authentication', exact: true }).click();
  await page.getByLabel('New password', { exact: true }).fill('Required-password-789!');
  await page.getByLabel('Confirm password', { exact: true }).fill('Required-password-789!');
  await page.getByRole('button', { name: 'Update password', exact: true }).click();
  await expect(page.getByText('Current password is required', { exact: true })).toBeVisible();
  await signOut(page);
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});

// covers: REQ-1-3
test('REQ-1-3: an incorrect current password leaves the old credential usable', async ({ page }) => {
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('link', { name: 'Password and authentication', exact: true }).click();
  await page.getByLabel('Current password', { exact: true }).fill('Not-the-password-1!');
  await page.getByLabel('New password', { exact: true }).fill('New-password-456!');
  await page.getByLabel('Confirm password', { exact: true }).fill('New-password-456!');
  await page.getByRole('button', { name: 'Update password', exact: true }).click();
  await expect(page.getByText('Current password is incorrect', { exact: true })).toBeVisible();
  await signOut(page);
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});

// covers: REQ-1-3
test('REQ-1-3: a mismatched confirmation is rejected and the old password still signs in', async ({ page }) => {
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('link', { name: 'Password and authentication', exact: true }).click();
  await page.getByLabel('Current password', { exact: true }).fill(FIXTURES.password);
  await page.getByLabel('New password', { exact: true }).fill('New-password-456!');
  await page.getByLabel('Confirm password', { exact: true }).fill('does-not-match');
  await page.getByRole('button', { name: 'Update password', exact: true }).click();
  await expect(page.getByText('Password confirmation does not match', { exact: true })).toBeVisible();
  await signOut(page);
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});
