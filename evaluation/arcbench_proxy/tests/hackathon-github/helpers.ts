import { expect, Page } from '@playwright/test';

export const FIXTURES = {
  username: 'alice-dev',
  email: 'alice.dev@example.test',
  password: 'Valid-password-123!',
  organization: 'Acme Demo',
  publicRepository: 'acme-docs',
  privateRepository: 'secret-research',
  openIssue: 'Improve onboarding',
  closedIssue: 'Legacy welcome text',
  openPullRequest: 'Improve onboarding',
  secondPullRequest: 'Fix search',
  teamName: 'frontend-team',
  member: 'bob-reviewer',
  labels: ['bug', 'documentation'],
  milestone: 'Q3 launch',
} as const;

export const ACCOUNT_PASSWORD = FIXTURES.password;

export async function openHome(page: Page): Promise<void> {
  await page.goto('/');
}

export async function signInAs(page: Page, username: string, password: string): Promise<void> {
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(username);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Account menu', exact: true })).toBeVisible();
}

export async function signIn(page: Page): Promise<void> {
  await signInAs(page, FIXTURES.username, FIXTURES.password);
}

export async function registerAccount(page: Page, username: string, email: string, password: string): Promise<void> {
  await openHome(page);
  await page.getByRole('link', { name: 'Sign in', exact: true }).click();
  await page.getByRole('link', { name: 'Create an account', exact: true }).click();
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Email', { exact: true }).fill(email);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByLabel('Confirm password', { exact: true }).fill(password);
  await page.getByRole('checkbox', { name: 'Agree to the terms', exact: true }).check();
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(page.getByLabel('Username or email', { exact: true })).toBeVisible();
}

export async function signOut(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Account menu', exact: true }).click();
  await page.getByRole('link', { name: 'Sign out', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm sign out', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Sign in', exact: true })).toBeVisible();
}

export async function openRepository(page: Page, name: string): Promise<void> {
  await openHome(page);
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill(name);
  await search.press('Enter');
  await page.getByRole('link', { name, exact: true }).first().click();
  await expect(page.getByRole('heading', { name: new RegExp(name, 'i') })).toBeVisible();
}

export async function openPublicRepository(page: Page): Promise<void> {
  await openRepository(page, FIXTURES.publicRepository);
}

export async function openOrganization(page: Page, name = FIXTURES.organization): Promise<void> {
  await openHome(page);
  await page.getByRole('link', { name, exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(name, 'i') })).toBeVisible();
}

export async function openOrganizationRepositories(page: Page, name = FIXTURES.organization): Promise<void> {
  await openOrganization(page, name);
  await page.getByRole('link', { name: 'Repositories', exact: true }).click();
}

export async function openIssues(page: Page, repository = FIXTURES.publicRepository): Promise<void> {
  await openRepository(page, repository);
  await page.getByRole('link', { name: 'Issues', exact: true }).click();
  await expect(page.getByRole('searchbox', { name: 'Search issues', exact: true })).toBeVisible();
}

export async function openPullRequests(page: Page, repository = FIXTURES.publicRepository): Promise<void> {
  await openRepository(page, repository);
  await page.getByRole('link', { name: 'Pull requests', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Open', exact: true })).toBeVisible();
}

export async function openRepositorySettings(page: Page, repository = FIXTURES.publicRepository): Promise<void> {
  await openRepository(page, repository);
  await page.getByRole('link', { name: 'Settings', exact: true }).click();
}

export async function createRepository(
  page: Page,
  name: string,
  options: { description?: string; visibility?: 'Public' | 'Private'; readme?: boolean } = {},
): Promise<void> {
  await signIn(page);
  await page.getByRole('link', { name: 'New repository', exact: true }).click();
  await page.getByLabel('Repository name', { exact: true }).fill(name);
  if (options.description) {
    await page.getByLabel('Description', { exact: true }).fill(options.description);
  }
  if (options.visibility === 'Public') {
    await page.getByRole('radio', { name: 'Public', exact: true }).check();
  } else if (options.visibility === 'Private') {
    await page.getByRole('radio', { name: 'Private', exact: true }).check();
  }
  if (options.readme) {
    await page.getByRole('checkbox', { name: 'Add a README file', exact: true }).check();
  }
  await page.getByRole('button', { name: 'Create repository', exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(name, 'i') })).toBeVisible();
}

export async function createBranch(page: Page, name: string, repository = FIXTURES.publicRepository): Promise<void> {
  await openRepository(page, repository);
  await page.getByRole('button', { name: /^Branch / }).click();
  const findBranch = page.getByRole('textbox', { name: 'Find branch', exact: true });
  await findBranch.fill(name);
  await page.getByRole('option', { name: `Create branch: ${name}`, exact: true }).click();
  await expect(page.getByRole('button', { name: `Branch ${name}` })).toBeVisible();
}

export async function commitNewFile(
  page: Page,
  fileName: string,
  content: string,
  message: string,
  branch?: string,
): Promise<void> {
  if (branch) {
    await page.getByRole('button', { name: /^Branch / }).click();
    const findBranch = page.getByRole('textbox', { name: 'Find branch', exact: true });
    await findBranch.fill(branch);
    await page.getByRole('option', { name: branch, exact: true }).click();
    await expect(page.getByRole('button', { name: `Branch ${branch}` })).toBeVisible();
  }
  await page.getByRole('button', { name: 'Add file', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Create new file', exact: true }).click();
  await page.getByLabel('File name', { exact: true }).fill(fileName);
  await page.getByRole('textbox', { name: 'File contents', exact: true }).fill(content);
  await page.getByLabel('Commit message', { exact: true }).fill(message);
  await page.getByRole('button', { name: 'Commit changes', exact: true }).click();
  await expect(page.getByText(content)).toBeVisible();
}

export async function createIssue(page: Page, title: string, description: string): Promise<void> {
  await openIssues(page);
  await page.getByRole('link', { name: 'New issue', exact: true }).click();
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByLabel('Description', { exact: true }).fill(description);
  await page.getByRole('button', { name: 'Submit new issue', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
}

export async function createPullRequest(
  page: Page,
  base: string,
  compare: string,
  title: string,
  description = '',
  repository = FIXTURES.publicRepository,
): Promise<void> {
  await openPullRequests(page, repository);
  await page.getByRole('link', { name: 'New pull request', exact: true }).click();
  await page.getByRole('combobox', { name: 'base', exact: true }).selectOption({ label: base });
  await page.getByRole('combobox', { name: 'compare', exact: true }).selectOption({ label: compare });
  await page.getByRole('button', { name: 'Compare changes', exact: true }).click();
  await page.getByLabel('Title', { exact: true }).fill(title);
  if (description) {
    await page.getByLabel('Description', { exact: true }).fill(description);
  }
  await page.getByRole('button', { name: 'Create pull request', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
}

// Adds an existing account to the seeded organization as a Member.
export async function addOrganizationMember(page: Page, username: string): Promise<void> {
  await openOrganization(page);
  await page.getByRole('link', { name: 'People', exact: true }).click();
  await page.getByRole('button', { name: 'Add member', exact: true }).click();
  await page.getByLabel('Username or email', { exact: true }).fill(username);
  await page.getByRole('combobox', { name: 'Role', exact: true }).selectOption({ label: 'Member' });
  await page.getByRole('button', { name: 'Add member', exact: true }).last().click();
  await expect(page.getByText(username, { exact: true }).first()).toBeVisible();
}

// Grants an account or team a repository role from Settings → Manage access.
export async function grantRepositoryAccess(
  page: Page,
  subject: string,
  role: string,
  repository = FIXTURES.publicRepository,
): Promise<void> {
  await openRepositorySettings(page, repository);
  await page.getByRole('link', { name: 'Manage access', exact: true }).click();
  await page.getByRole('button', { name: 'Add people or teams', exact: true }).click();
  await page.getByRole('textbox', { name: 'Search', exact: true }).fill(subject);
  await page.getByRole('option', { name: new RegExp(subject) }).first().click();
  await page.getByRole('combobox', { name: 'Role', exact: true }).selectOption({ label: role });
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  await expect(page.getByText(subject, { exact: true })).toBeVisible();
}

// Registers a fresh reviewer account, makes it an organization member with
// Write on the seeded repository, then restores the alice session.
export async function provisionReviewer(page: Page, username: string): Promise<void> {
  await signOut(page);
  await registerAccount(page, username, `${username}@example.test`, 'Reviewer-password-123!');
  await signOut(page);
  await signIn(page);
  await addOrganizationMember(page, username);
  await grantRepositoryAccess(page, username, 'Write');
  await signOut(page);
}
