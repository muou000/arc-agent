import { test, expect } from '@playwright/test';
import { cell, createBlankWorkbook, expectGrid, FIXTURES } from './helpers';

// covers: REQ-1-2-1
test('REQ-1-2-1: create and restore a blank workbook', async ({ page }) => {
  await createBlankWorkbook(page);
  await expectGrid(page);
  const tab = page.getByRole('tab', { name: FIXTURES.worksheet, exact: true });
  await expect(tab).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
  const editorUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(editorUrl);
  await expect(tab).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
});
