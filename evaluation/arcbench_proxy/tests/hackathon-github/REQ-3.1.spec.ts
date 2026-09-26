import { test, expect } from '@playwright/test';
import { signIn } from './helpers';

// covers: REQ-3-2-1
test('REQ-3-2-1: create a private initialized repository and preserve it', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: 'New repository', exact: true }).click();
  const name = 'proxy-repository-' + String(Date.now());
  await page.getByLabel('Repository name', { exact: true }).fill(name);
  await page.getByLabel('Description', { exact: true }).fill('Repository created by Playwright');
  await page.getByRole('radio', { name: 'Private', exact: true }).check();
  await page.getByRole('checkbox', { name: 'Add a README file', exact: true }).check();
  await page.getByRole('button', { name: 'Create repository', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(name, 'i') })).toBeVisible();
  await expect(page.getByText('Private', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'README.md', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(name, 'i') })).toBeVisible();
  await expect(page.getByRole('link', { name: 'README.md', exact: true })).toBeVisible();
});
