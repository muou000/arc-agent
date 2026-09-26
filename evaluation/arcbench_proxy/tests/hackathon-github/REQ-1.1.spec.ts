import { test, expect } from '@playwright/test';
import { FIXTURES, openHome, registerAccount, signInAs, signOut } from './helpers';

// covers: REQ-1-1-1
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

// covers: REQ-1-1-1
test('REQ-1-1-1: all invalid fields report their messages together and keep input', async ({ page }) => {
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByRole('link', { name: 'Create an account', exact: true }).click();
  await page.getByLabel('Username', { exact: true }).fill('-bad-user-');
  await page.getByLabel('Email', { exact: true }).fill('not-an-email');
  await page.getByLabel('Password', { exact: true }).fill('short');
  await page.getByLabel('Confirm password', { exact: true }).fill('different');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByText('Username format is invalid')).toBeVisible();
  await expect(page.getByText('Email format is invalid')).toBeVisible();
  await expect(page.getByText('Password requirements are not satisfied')).toBeVisible();
  await expect(page.getByText('Agree to terms is required')).toBeVisible();
  await expect(page.getByLabel('Username', { exact: true })).toHaveValue('-bad-user-');
  await expect(page.getByLabel('Email', { exact: true })).toHaveValue('not-an-email');
  await expect(page.getByLabel('Password', { exact: true })).toHaveValue('');
  await expect(page.getByRole('checkbox', { name: 'Agree to the terms', exact: true })).not.toBeChecked();
});

// covers: REQ-1-1-1
test('REQ-1-1-1: a duplicate username is rejected while the email stays unused', async ({ page }) => {
  const suffix = String(Date.now());
  const username = 'proxy-dup-' + suffix;
  await registerAccount(page, username, `${username}-a@example.test`, FIXTURES.password);
  await signOut(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByRole('link', { name: 'Create an account', exact: true }).click();
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Email', { exact: true }).fill(`${username}-b@example.test`);
  await page.getByLabel('Password', { exact: true }).fill(FIXTURES.password);
  await page.getByLabel('Confirm password', { exact: true }).fill(FIXTURES.password);
  await page.getByRole('checkbox', { name: 'Agree to the terms', exact: true }).check();
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByText('Username already exists')).toBeVisible();
  await expect(page.getByLabel('Username', { exact: true })).toHaveValue(username);
  await expect(page.getByLabel('Email', { exact: true })).toHaveValue(`${username}-b@example.test`);
});

// covers: REQ-1-1-1
test('REQ-1-1-1: a compliant registration is immediately reusable for sign-in', async ({ page }) => {
  const suffix = String(Date.now());
  const username = 'pw-user-' + suffix;
  const email = `${username}@example.test`;
  await registerAccount(page, username, email, FIXTURES.password);
  await signInAs(page, email, FIXTURES.password);
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});

// covers: REQ-1-1-2
test('REQ-1-1-2: sign in with the seeded email and keep the session after reload', async ({ page }) => {
  await signInAs(page, FIXTURES.email, FIXTURES.password);
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});

// covers: REQ-1-1-2
test('REQ-1-1-2: a wrong password shows the generic failure message', async ({ page }) => {
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(FIXTURES.username);
  await page.getByLabel('Password', { exact: true }).fill('Wrong-password-123!');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByText('Invalid credentials', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toHaveCount(0);
});

// covers: REQ-1-1-2
test('REQ-1-1-2: an unknown account shows the same generic failure message', async ({ page }) => {
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill('no-such-account-xyz');
  await page.getByLabel('Password', { exact: true }).fill(FIXTURES.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByText('Invalid credentials', { exact: true })).toBeVisible();
  await expect(page.getByText(/account does not exist|unknown user/i)).toHaveCount(0);
});

// covers: REQ-1-1-3
test('REQ-1-1-3: recover a registered account with the fixed code and a compliant password', async ({ page }) => {
  const suffix = String(Date.now());
  const username = 'pw-recover-' + suffix;
  const email = `${username}@example.test`;
  await registerAccount(page, username, email, FIXTURES.password);
  await signOut(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByRole('link', { name: 'Forgot password', exact: true }).click();
  await page.getByLabel('Email', { exact: true }).fill(email);
  await page.getByRole('button', { name: 'Send reset link', exact: true }).click();
  await expect(page.getByText('123456', { exact: true })).toBeVisible();
  await page.getByLabel('Verification code', { exact: true }).fill('123456');
  await page.getByLabel('New password', { exact: true }).fill('Replacement-password-456!');
  await page.getByLabel('Confirm password', { exact: true }).fill('Replacement-password-456!');
  await page.getByRole('button', { name: 'Reset password', exact: true }).click();
  await expect(page.getByText('Password updated', { exact: true })).toBeVisible();
  await signInAs(page, email, 'Replacement-password-456!');
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});

// covers: REQ-1-1-3
test('REQ-1-1-3: a wrong verification code changes nothing for the account', async ({ page }) => {
  const suffix = String(Date.now());
  const username = 'pw-code-' + suffix;
  const email = `${username}@example.test`;
  await registerAccount(page, username, email, FIXTURES.password);
  await signOut(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByRole('link', { name: 'Forgot password', exact: true }).click();
  await page.getByLabel('Email', { exact: true }).fill(email);
  await page.getByRole('button', { name: 'Send reset link', exact: true }).click();
  await page.getByLabel('Verification code', { exact: true }).fill('000000');
  await page.getByLabel('New password', { exact: true }).fill('Replacement-password-456!');
  await page.getByLabel('Confirm password', { exact: true }).fill('Replacement-password-456!');
  await page.getByRole('button', { name: 'Reset password', exact: true }).click();
  await expect(page.getByText('Verification code is invalid')).toBeVisible();
  await signInAs(page, email, FIXTURES.password);
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
});

// covers: REQ-1-1-3
test('REQ-1-1-3: an unknown email enters the same step without disclosing existence', async ({ page }) => {
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByRole('link', { name: 'Forgot password', exact: true }).click();
  await page.getByLabel('Email', { exact: true }).fill('nobody-unknown@example.test');
  await page.getByRole('button', { name: 'Send reset link', exact: true }).click();
  await expect(page.getByText('123456', { exact: true })).toBeVisible();
  await expect(page.getByText(/email not found|account does not exist/i)).toHaveCount(0);
  await expect(page.getByLabel('New password', { exact: true })).toHaveAttribute('type', 'password');
});
