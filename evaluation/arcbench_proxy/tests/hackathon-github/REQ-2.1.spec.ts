import { test, expect } from '@playwright/test';
import { FIXTURES, openHome } from './helpers';

// covers: REQ-2-1-1
test('REQ-2-1-1: filter public organization repositories without leaking private ones', async ({ page }) => {
  await openHome(page);
  await page.getByRole('link', { name: FIXTURES.organization, exact: true }).click();
  await page.getByRole('link', { name: 'Repositories', exact: true }).click();
  const filter = page.getByLabel('Find a repository', { exact: true });
  await filter.fill(FIXTURES.publicRepository);
  await expect(page.getByRole('link', { name: FIXTURES.publicRepository, exact: true })).toBeVisible();
  await page.getByRole('link', { name: FIXTURES.publicRepository, exact: true }).click();
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.organization + '/' + FIXTURES.publicRepository, 'i') })).toBeVisible();
  await page.goBack();
  await filter.fill(FIXTURES.privateRepository);
  await expect(page.getByRole('link', { name: FIXTURES.privateRepository, exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('link', { name: FIXTURES.privateRepository, exact: true })).toHaveCount(0);
});
