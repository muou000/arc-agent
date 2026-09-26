import { test } from '@playwright/test';
import { createBlankWorkbook, editCell, expectCell } from './helpers';

// covers: REQ-4-1-1
test('REQ-4-1-1: calculate references and aggregates and preserve formulas', async ({ page }) => {
  await createBlankWorkbook(page);
  await editCell(page, 'A1', '2');
  await editCell(page, 'B1', '3');
  await editCell(page, 'C1', '=A1+B1');
  await editCell(page, 'D1', '=SUM(A1:B1)');
  await expectCell(page, 'C1', '5');
  await expectCell(page, 'D1', '5');
  await page.reload();
  await expectCell(page, 'C1', '5');
  await expectCell(page, 'D1', '5');
  await page.getByRole('gridcell', { name: 'C1', exact: true }).click();
  await page.getByRole('textbox', { name: 'Formula bar', exact: true }).toHaveValue('=A1+B1');
});
