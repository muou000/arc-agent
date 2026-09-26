import { test } from '@playwright/test';
import { editCell, expectCell, openWorkbook } from './helpers';

// covers: REQ-3-1-1
test('REQ-3-1-1: edit ordinary and formula cells through the formula bar', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '2');
  await editCell(page, 'E1', '=D1*2');
  await expectCell(page, 'E1', '4');
  await page.reload();
  await expectCell(page, 'D1', '2');
  await expectCell(page, 'E1', '4');
  await page.getByRole('gridcell', { name: 'E1', exact: true }).click();
  await page.getByRole('textbox', { name: 'Formula bar', exact: true }).toHaveValue('=D1*2');
});
