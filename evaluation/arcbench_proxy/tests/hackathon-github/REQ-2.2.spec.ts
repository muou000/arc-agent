import { expect, Page, test } from '@playwright/test';
import { FIXTURES, openOrganization, registerAccount, signIn, signInAs, signOut } from './helpers';

async function openTeam(page: Page, teamName: string) {
  await openOrganization(page);
  await page.getByRole('link', { name: 'Teams', exact: true }).click();
  await page.getByRole('link', { name: teamName, exact: true }).first().click();
}

// covers: REQ-2-2-1
test('REQ-2-2-1: an owner creates a team without description or parent', async ({ page }) => {
  await signIn(page);
  await openOrganization(page);
  await page.getByRole('link', { name: 'Teams', exact: true }).click();
  await page.getByRole('link', { name: 'New team', exact: true }).click();
  const teamName = 'pw-team-' + String(Date.now());
  await page.getByLabel('Team name', { exact: true }).fill(teamName);
  await page.getByRole('button', { name: 'Create team', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(teamName, 'i') })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: new RegExp(teamName, 'i') })).toBeVisible();
});

// covers: REQ-2-2-1
test('REQ-2-2-1: a malformed team name is rejected and no team appears', async ({ page }) => {
  await signIn(page);
  await openOrganization(page);
  await page.getByRole('link', { name: 'Teams', exact: true }).click();
  await page.getByRole('link', { name: 'New team', exact: true }).click();
  await page.getByLabel('Team name', { exact: true }).fill('bad team name!');
  await page.getByRole('button', { name: 'Create team', exact: true }).click();
  await expect(page.getByText('Team name format is invalid')).toBeVisible();
  await openOrganization(page);
  await page.getByRole('link', { name: 'Teams', exact: true }).click();
  await expect(page.getByRole('link', { name: 'bad team name!', exact: true })).toHaveCount(0);
});

// covers: REQ-2-2-1
test('REQ-2-2-1: the seeded team page is titled organization/team with a Members tab', async ({ page }) => {
  await signIn(page);
  await openTeam(page, FIXTURES.teamName);
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.teamName, 'i') })).toBeVisible();
  await expect(page.getByText(new RegExp(FIXTURES.organization, 'i')).first()).toBeVisible();
  await page.getByRole('link', { name: 'Members', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Add member', exact: true })).toBeVisible();
});

// covers: REQ-2-2-2
test('REQ-2-2-2: an owner adds and immediately removes a team member', async ({ page }) => {
  await signIn(page);
  await openTeam(page, FIXTURES.teamName);
  await page.getByRole('link', { name: 'Members', exact: true }).click();
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await page.getByLabel('Username', { exact: true }).fill(FIXTURES.member);
  await page.getByRole('button', { name: 'Add member', exact: true }).last().click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: `Remove ${FIXTURES.member}`, exact: true }).click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toHaveCount(0);
});

// covers: REQ-2-2-2
test('REQ-2-2-2: an owner assigns a parent team and the choice persists', async ({ page }) => {
  await signIn(page);
  await openOrganization(page);
  await page.getByRole('link', { name: 'Teams', exact: true }).click();
  const suffix = String(Date.now());
  const parentName = 'pw-parent-' + suffix;
  const childName = 'pw-child-' + suffix;
  for (const teamName of [parentName, childName]) {
    await page.getByRole('link', { name: 'New team', exact: true }).click();
    await page.getByLabel('Team name', { exact: true }).fill(teamName);
    await page.getByRole('button', { name: 'Create team', exact: true }).click();
    await expect(page.getByRole('heading', { name: new RegExp(teamName, 'i') })).toBeVisible();
  }
  await page.getByRole('link', { name: childName, exact: true }).click();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('combobox', { name: 'Parent team', exact: true }).selectOption({ label: parentName });
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.reload();
  await expect(page.getByRole('combobox', { name: 'Parent team', exact: true })).toContainText(parentName);
});

// covers: REQ-2-2-2
test('REQ-2-2-2: a cyclic hierarchy is rejected and the original parent is kept', async ({ page }) => {
  await signIn(page);
  await openOrganization(page);
  await page.getByRole('link', { name: 'Teams', exact: true }).click();
  const suffix = String(Date.now());
  const outerName = 'pw-outer-' + suffix;
  const innerName = 'pw-inner-' + suffix;
  await page.getByRole('link', { name: 'New team', exact: true }).click();
  await page.getByLabel('Team name', { exact: true }).fill(outerName);
  await page.getByRole('button', { name: 'Create team', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(outerName, 'i') })).toBeVisible();
  await page.getByRole('link', { name: 'New team', exact: true }).click();
  await page.getByLabel('Team name', { exact: true }).fill(innerName);
  await page.getByRole('button', { name: 'Create team', exact: true }).click();
  await page.getByRole('link', { name: innerName, exact: true }).click();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  await page.getByRole('combobox', { name: 'Parent team', exact: true }).selectOption({ label: outerName });
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByRole('link', { name: outerName, exact: true }).click();
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
  const selector = page.getByRole('combobox', { name: 'Parent team', exact: true });
  const original = await selector.inputValue();
  await selector.selectOption({ label: innerName });
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Cyclic team hierarchy is not allowed')).toBeVisible();
  await page.reload();
  await expect(page.getByRole('combobox', { name: 'Parent team', exact: true })).toHaveValue(original);
});

// covers: REQ-2-2-3
test('REQ-2-2-3: an owner adds a registered account as a Member', async ({ page }) => {
  const suffix = String(Date.now());
  const newMember = 'pw-member-' + suffix;
  await signIn(page);
  await registerAccount(page, newMember, `${newMember}@example.test`, FIXTURES.password);
  await signOut(page);
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await openOrganization(page);
  await page.getByRole('link', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(newMember);
  await page.getByRole('combobox', { name: 'Role', exact: true }).selectOption({ label: 'Member' });
  await page.getByRole('button', { name: 'Add member', exact: true }).last().click();
  await expect(page.getByText(newMember, { exact: true })).toBeVisible();
  await expect(page.getByText('Pending', { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(newMember, { exact: true })).toBeVisible();
});

// covers: REQ-2-2-3
test('REQ-2-2-3: adding an existing member keeps the form open with its message', async ({ page }) => {
  await signIn(page);
  await openOrganization(page);
  await page.getByRole('link', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(FIXTURES.member);
  await page.getByRole('button', { name: 'Add member', exact: true }).last().click();
  await expect(page.getByText('Account is already a member')).toBeVisible();
  await expect(page.getByLabel('Username or email', { exact: true })).toBeVisible();
});

// covers: REQ-2-2-3
test('REQ-2-2-3: an unknown username reports Account not found', async ({ page }) => {
  await signIn(page);
  await openOrganization(page);
  await page.getByRole('link', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill('unknown-reviewer');
  await page.getByRole('button', { name: 'Add member', exact: true }).last().click();
  await expect(page.getByText('Account not found')).toBeVisible();
  await expect(page.getByLabel('Username or email', { exact: true })).toBeVisible();
});

// covers: REQ-2-2-4
test('REQ-2-2-4: an owner removes a member through the menu and confirmation', async ({ page }) => {
  await signIn(page);
  await openOrganization(page);
  await page.getByRole('link', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: `Member menu ${FIXTURES.member}`, exact: true }).click();
  await page.getByRole('menuitem', { name: 'Remove from organization', exact: true }).click();
  await page.getByRole('button', { name: 'Remove', exact: true }).click();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(FIXTURES.member, { exact: true })).toHaveCount(0);
});

// covers: REQ-2-2-4
test('REQ-2-2-4: a non-owner sees no member menu or removal entry', async ({ page }) => {
  const suffix = String(Date.now());
  const viewer = 'pw-viewer-' + suffix;
  await signIn(page);
  await registerAccount(page, viewer, `${viewer}@example.test`, FIXTURES.password);
  await signOut(page);
  await signInAs(page, FIXTURES.username, FIXTURES.password);
  await openOrganization(page);
  await page.getByRole('link', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(viewer);
  await page.getByRole('combobox', { name: 'Role', exact: true }).selectOption({ label: 'Member' });
  await page.getByRole('button', { name: 'Add member', exact: true }).last().click();
  await expect(page.getByText(viewer, { exact: true })).toBeVisible();
  await signOut(page);
  await signInAs(page, viewer, FIXTURES.password);
  await openOrganization(page);
  await page.getByRole('link', { name: 'People', exact: true }).click();
  await expect(page.getByRole('button', { name: `Member menu ${FIXTURES.member}`, exact: true })).toHaveCount(0);
  await expect(page.getByRole('menuitem', { name: 'Remove from organization', exact: true })).toHaveCount(0);
});

// covers: REQ-2-2-4
test('REQ-2-2-4: a removed account can be added again as a member', async ({ page }) => {
  await signIn(page);
  await openOrganization(page);
  await page.getByRole('link', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(FIXTURES.member);
  await page.getByRole('combobox', { name: 'Role', exact: true }).selectOption({ label: 'Member' });
  await page.getByRole('button', { name: 'Add member', exact: true }).last().click();
  await expect(page.getByText(FIXTURES.member, { exact: true }).first()).toBeVisible();
});
