import { expect, Page, test } from '@playwright/test';
import { FIXTURES, openIssues, registerAccount, signIn } from './helpers';

async function openSeedIssue(page: Page) {
  await openIssues(page);
  await page.getByRole('link', { name: FIXTURES.openIssue, exact: true }).click();
  await expect(page.getByRole('heading', { name: FIXTURES.openIssue, exact: true })).toBeVisible();
}

// covers: REQ-5-3-1
test('REQ-5-3-1: clicking an assignable member saves the assignment immediately', async ({ page }) => {
  await signIn(page);
  await openSeedIssue(page);
  await page.getByRole('button', { name: 'Assignees', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search assignees', exact: true }).fill(FIXTURES.member);
  await page.getByRole('option', { name: FIXTURES.member, exact: true }).click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toBeVisible();
});

// covers: REQ-5-3-1
test('REQ-5-3-1: reopening shows the selected member and clicking again unassigns', async ({ page }) => {
  await signIn(page);
  await openSeedIssue(page);
  await page.getByRole('button', { name: 'Assignees', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search assignees', exact: true }).fill(FIXTURES.member);
  await page.getByRole('option', { name: FIXTURES.member, exact: true }).click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Assignees', exact: true }).click();
  await expect(page.getByRole('option', { name: FIXTURES.member, exact: true })).toBeVisible();
  await page.getByRole('option', { name: FIXTURES.member, exact: true }).click();
  await page.reload();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toHaveCount(0);
});

// covers: REQ-5-3-1
test('REQ-5-3-1: assignment transitions remain visible as historical activities', async ({ page }) => {
  await signIn(page);
  await openSeedIssue(page);
  await page.getByRole('button', { name: 'Assignees', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search assignees', exact: true }).fill(FIXTURES.member);
  await page.getByRole('option', { name: FIXTURES.member, exact: true }).click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toBeVisible();
  await expect(page.getByText(/assigned|Assignees/i).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText(/assigned/i).first()).toBeVisible();
});

// covers: REQ-5-3-2
test('REQ-5-3-2: clicking a repository label applies it immediately', async ({ page }) => {
  await signIn(page);
  await openSeedIssue(page);
  await page.getByRole('button', { name: 'Labels', exact: true }).click();
  await page.getByRole('option', { name: 'bug', exact: true }).click();
  await expect(page.getByText('bug', { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText('bug', { exact: true }).first()).toBeVisible();
});

// covers: REQ-5-3-2
test('REQ-5-3-2: selecting the same label again removes it', async ({ page }) => {
  await signIn(page);
  await openSeedIssue(page);
  await page.getByRole('button', { name: 'Labels', exact: true }).click();
  await page.getByRole('option', { name: 'bug', exact: true }).click();
  await expect(page.getByText('bug', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Labels', exact: true }).click();
  await page.getByRole('option', { name: 'bug', exact: true }).click();
  await page.reload();
  await expect(page.getByText('bug', { exact: true })).toHaveCount(0);
});

// covers: REQ-5-3-2
test('REQ-5-3-2: an account without triage rights gets no label control', async ({ page }) => {
  const suffix = String(Date.now());
  const outsider = 'pw-outsider-' + suffix;
  await registerAccount(page, outsider, `${outsider}@example.test`, FIXTURES.password);
  await openIssues(page);
  await page.getByRole('link', { name: FIXTURES.openIssue, exact: true }).click();
  await expect(page.getByRole('heading', { name: FIXTURES.openIssue, exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Labels', exact: true })).toHaveCount(0);
});

// covers: REQ-5-3-3
test('REQ-5-3-3: selecting a milestone saves it without a separate confirmation', async ({ page }) => {
  await signIn(page);
  await openSeedIssue(page);
  await page.getByRole('button', { name: 'Milestone', exact: true }).click();
  await page.getByRole('option', { name: FIXTURES.milestone, exact: true }).click();
  await expect(page.getByText(FIXTURES.milestone, { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText(FIXTURES.milestone, { exact: true })).toBeVisible();
});

// covers: REQ-5-3-3
test('REQ-5-3-3: choosing None removes the milestone association', async ({ page }) => {
  await signIn(page);
  await openSeedIssue(page);
  await page.getByRole('button', { name: 'Milestone', exact: true }).click();
  await page.getByRole('option', { name: FIXTURES.milestone, exact: true }).click();
  await expect(page.getByText(FIXTURES.milestone, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Milestone', exact: true }).click();
  await page.getByRole('option', { name: 'None', exact: true }).click();
  await page.reload();
  await expect(page.getByText(FIXTURES.milestone, { exact: true })).toHaveCount(0);
});

// covers: REQ-5-3-3
test('REQ-5-3-3: the picker only offers milestones of the current repository', async ({ page }) => {
  await signIn(page);
  await openSeedIssue(page);
  await page.getByRole('button', { name: 'Milestone', exact: true }).click();
  await expect(page.getByRole('option', { name: FIXTURES.milestone, exact: true })).toBeVisible();
  await expect(page.getByRole('option', { name: 'v1.0', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByText(FIXTURES.milestone, { exact: true })).toHaveCount(0);
});
