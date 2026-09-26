import { test, expect } from '@playwright/test';
import { FIXTURES, createIssue, openIssues, openRepository, signIn } from './helpers';

// covers: REQ-5-2-1
test('REQ-5-2-1: creating an issue opens its detail with title and description', async ({ page }) => {
  await signIn(page);
  await openIssues(page);
  await page.getByRole('link', { name: 'New issue', exact: true }).click();
  const title = 'Proxy issue ' + String(Date.now());
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByLabel('Description', { exact: true }).fill('Created by the proxy acceptance suite.');
  await page.getByRole('button', { name: 'Submit new issue', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await expect(page.getByText('Created by the proxy acceptance suite.')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
});

// covers: REQ-5-2-1
test('REQ-5-2-1: a blank title is rejected and creates no issue', async ({ page }) => {
  await signIn(page);
  await openIssues(page);
  await page.getByRole('link', { name: 'New issue', exact: true }).click();
  await page.getByLabel('Title', { exact: true }).fill('   ');
  await page.getByRole('button', { name: 'Submit new issue', exact: true }).click();
  await expect(page.getByText('Title is required')).toBeVisible();
  await expect(page.getByRole('heading', { name: /proxy issue/i })).toHaveCount(0);
});

// covers: REQ-5-2-1
test('REQ-5-2-1: an issue can be created without a description', async ({ page }) => {
  await signIn(page);
  const title = 'Bare issue ' + String(Date.now());
  await createIssue(page, title, '');
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await openIssues(page);
  await page.getByRole('searchbox', { name: 'Search issues', exact: true }).fill(title);
  await expect(page.getByRole('link', { name: title, exact: true })).toBeVisible();
});

// covers: REQ-5-2-2
test('REQ-5-2-2: the title edit updates the heading, list and reload state', async ({ page }) => {
  await signIn(page);
  const title = 'Editable issue ' + String(Date.now());
  const updated = title + ' (renamed)';
  await createIssue(page, title, 'original description');
  await page.getByRole('button', { name: 'Edit issue title', exact: true }).click();
  await page.getByLabel('Issue title', { exact: true }).fill(updated);
  await page.getByRole('button', { name: 'Save issue title', exact: true }).click();
  await expect(page.getByRole('heading', { name: updated, exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: updated, exact: true })).toBeVisible();
  await openIssues(page);
  await page.getByRole('searchbox', { name: 'Search issues', exact: true }).fill(updated);
  await expect(page.getByRole('link', { name: updated, exact: true })).toBeVisible();
});

// covers: REQ-5-2-2
test('REQ-5-2-2: the description edit is a separate action and persists', async ({ page }) => {
  await signIn(page);
  const title = 'Describable issue ' + String(Date.now());
  await createIssue(page, title, 'original description');
  await page.getByRole('button', { name: 'Edit issue description', exact: true }).click();
  await page.getByLabel('Issue description', { exact: true }).fill('rewritten description body');
  await page.getByRole('button', { name: 'Save issue description', exact: true }).click();
  await expect(page.getByText('rewritten description body')).toBeVisible();
  await page.reload();
  await expect(page.getByText('rewritten description body')).toBeVisible();
  await expect(page.getByText('original description')).toHaveCount(0);
});

// covers: REQ-5-2-2
test('REQ-5-2-2: an empty replacement title is rejected and the original returns', async ({ page }) => {
  await signIn(page);
  await openIssues(page);
  await page.getByRole('searchbox', { name: 'Search issues', exact: true }).fill('Original issue title');
  await page.getByRole('link', { name: 'Original issue title', exact: true }).first().click();
  await page.getByRole('button', { name: 'Edit issue title', exact: true }).click();
  await page.getByLabel('Issue title', { exact: true }).fill('   ');
  await page.getByRole('button', { name: 'Save issue title', exact: true }).click();
  await expect(page.getByText('Title is required')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Original issue title', exact: true })).toBeVisible();
});

// covers: REQ-5-2-3
test('REQ-5-2-3: a comment appears with its author and survives reload', async ({ page }) => {
  await signIn(page);
  const title = 'Commented issue ' + String(Date.now());
  await createIssue(page, title, 'comment target');
  const body = 'A proxy comment body ' + String(Date.now());
  await page.getByLabel('Comment', { exact: true }).fill(body);
  await page.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(page.getByText(body)).toBeVisible();
  await expect(page.getByText(new RegExp(FIXTURES.username))).toBeVisible();
  await page.reload();
  await expect(page.getByText(body)).toBeVisible();
});

// covers: REQ-5-2-3
test('REQ-5-2-3: a whitespace-only comment is not accepted', async ({ page }) => {
  await signIn(page);
  const title = 'Blank comment issue ' + String(Date.now());
  await createIssue(page, title, 'blank target');
  const editor = page.getByLabel('Comment', { exact: true });
  await editor.fill('   ');
  const button = page.getByRole('button', { name: 'Comment', exact: true });
  const disabled = await button.isDisabled();
  if (!disabled) {
    await button.click();
    await expect(page.getByText('Comment is required')).toBeVisible();
  }
  const articles = page.getByRole('article');
  const before = await articles.count();
  await page.reload();
  await expect(page.getByRole('article')).toHaveCount(before);
});

// covers: REQ-5-2-3
test('REQ-5-2-3: submitted comments use article semantics and stay stable', async ({ page }) => {
  await signIn(page);
  const title = 'Article issue ' + String(Date.now());
  await createIssue(page, title, 'article target');
  const first = 'first article body';
  await page.getByLabel('Comment', { exact: true }).fill(first);
  await page.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(page.getByRole('article').filter({ hasText: first })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('article').filter({ hasText: first })).toBeVisible();
  await expect(page.getByRole('article').first()).toContainText(first);
});
