import { test, expect } from '@playwright/test';
import { FIXTURES, expectCell, expectGrid, openWorkbook } from './helpers';

// covers: REQ-1-1-1
test('REQ-1-1-1: open and restore a seeded workbook', async ({ page }) => {
  await openWorkbook(page);
  await expectGrid(page);
  await expect(page.getByRole('tab', { name: FIXTURES.worksheet, exact: true })).toHaveAttribute('aria-selected', 'true');
  await expectCell(page, 'A1', 'Region');
  const editorUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(editorUrl);
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.workbook, 'i') })).toBeVisible();
  await expectCell(page, 'A1', 'Region');
});
