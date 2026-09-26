import { test, expect } from '@playwright/test';
import { FIXTURES, openHome, signIn, signInAs } from './helpers';

// covers: REQ-2-1-1
test('REQ-2-1-1: a signed-in member can filter and open the private repository', async ({ page }) => {
  await signIn(page);
  await openHome(page);
  await page.getByRole('link', { name: FIXTURES.organization, exact: true }).click();
  await page.getByRole('link', { name: 'Repositories', exact: true }).click();
  const filter = page.getByLabel('Find a repository', { exact: true });
  await filter.fill(FIXTURES.privateRepository);
  const link = page.getByRole('link', { name: FIXTURES.privateRepository, exact: true });
  await expect(link).toBeVisible();
  await link.click();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.privateRepository, 'i') })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.privateRepository, 'i') })).toBeVisible();
});

// covers: REQ-2-1-1
test('REQ-2-1-1: browser back keeps the public filtered result available', async ({ page }) => {
  await openHome(page);
  await page.getByRole('link', { name: FIXTURES.organization, exact: true }).click();
  await page.getByRole('link', { name: 'Repositories', exact: true }).click();
  const filter = page.getByLabel('Find a repository', { exact: true });
  await filter.fill(FIXTURES.publicRepository);
  await page.getByRole('link', { name: FIXTURES.publicRepository, exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.publicRepository, 'i') })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('link', { name: FIXTURES.publicRepository, exact: true })).toBeVisible();
});

// covers: REQ-2-1-2
test('REQ-2-1-2: create an organization and find it under Your organizations', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Your organizations', exact: true }).click();
  await page.getByRole('link', { name: 'New organization', exact: true }).click();
  const name = 'pw-org-' + String(Date.now());
  await page.getByLabel('Organization name', { exact: true }).fill(name);
  await page.getByLabel('Display name', { exact: true }).fill('Proxy Organization');
  await page.getByRole('button', { name: 'Create organization', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(name, 'i') })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(name, 'i') })).toBeVisible();
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Your organizations', exact: true }).click();
  await expect(page.getByRole('link', { name, exact: true })).toBeVisible();
});

// covers: REQ-2-1-2
test('REQ-2-1-2: an existing organization identifier is rejected even without a display name', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Your organizations', exact: true }).click();
  await page.getByRole('link', { name: 'New organization', exact: true }).click();
  await page.getByLabel('Organization name', { exact: true }).fill(FIXTURES.organization);
  await page.getByRole('button', { name: 'Create organization', exact: true }).click();
  await expect(page.getByText('Organization name already exists')).toBeVisible();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.organization, 'i') })).toHaveCount(0);
});

// covers: REQ-2-1-2
test('REQ-2-1-2: a malformed identifier and a blank display name report field errors', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Your organizations', exact: true }).click();
  await page.getByRole('link', { name: 'New organization', exact: true }).click();
  await page.getByLabel('Organization name', { exact: true }).fill('-invalid-organization');
  await page.getByLabel('Display name', { exact: true }).fill('   ');
  await page.getByRole('button', { name: 'Create organization', exact: true }).click();
  await expect(page.getByText('Organization name format is invalid')).toBeVisible();
  await expect(page.getByText('Display name is required')).toBeVisible();
  await expect(page.getByLabel('Organization name', { exact: true })).toHaveValue('-invalid-organization');
});
