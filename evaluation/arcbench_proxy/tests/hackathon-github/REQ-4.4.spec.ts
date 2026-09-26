import { test, expect } from '@playwright/test';
import { FIXTURES, openRepository, signIn } from './helpers';

// covers: REQ-4-4
test('REQ-4-4: creating a file commits it with its message and shows the content', async ({ page }) => {
  await signIn(page);
  await openRepository(page, FIXTURES.publicRepository);
  const fileName = 'pw-file-' + String(Date.now()) + '.md';
  const message = `Add ${fileName}`;
  await page.getByRole('button', { name: 'Add file', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Create new file', exact: true }).click();
  await page.getByLabel('File name', { exact: true }).fill(fileName);
  await page.getByRole('textbox', { name: 'File contents', exact: true }).fill('Proxy acceptance content');
  await page.getByLabel('Commit message', { exact: true }).fill(message);
  await page.getByRole('button', { name: 'Commit changes', exact: true }).click();
  await expect(page.getByText('Proxy acceptance content')).toBeVisible();
  await page.getByRole('link', { name: 'Commits', exact: true }).click();
  await expect(page.getByText(message)).toBeVisible();
});

// covers: REQ-4-4
test('REQ-4-4: an invalid path with no message is rejected without saving', async ({ page }) => {
  await signIn(page);
  await openRepository(page, FIXTURES.publicRepository);
  await page.getByRole('button', { name: 'Add file', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Create new file', exact: true }).click();
  await page.getByLabel('File name', { exact: true }).fill('../invalid.md');
  await page.getByRole('textbox', { name: 'File contents', exact: true }).fill('must not be saved');
  await page.getByRole('button', { name: 'Commit changes', exact: true }).click();
  await expect(page.getByText('Invalid file path').or(page.getByText('Commit message is required')).first()).toBeVisible();
  await page.getByRole('link', { name: 'Code', exact: true }).click();
  await expect(page.getByRole('link', { name: 'invalid.md', exact: true })).toHaveCount(0);
});

// covers: REQ-4-4
test('REQ-4-4: an empty commit message is rejected for a valid path', async ({ page }) => {
  await signIn(page);
  await openRepository(page, FIXTURES.publicRepository);
  const fileName = 'pw-nomsg-' + String(Date.now()) + '.md';
  await page.getByRole('button', { name: 'Add file', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Create new file', exact: true }).click();
  await page.getByLabel('File name', { exact: true }).fill(fileName);
  await page.getByRole('textbox', { name: 'File contents', exact: true }).fill('untitled commit');
  await page.getByRole('button', { name: 'Commit changes', exact: true }).click();
  await expect(page.getByText('Commit message is required')).toBeVisible();
  await page.getByRole('link', { name: 'Code', exact: true }).click();
  await expect(page.getByRole('link', { name: fileName, exact: true })).toHaveCount(0);
});
