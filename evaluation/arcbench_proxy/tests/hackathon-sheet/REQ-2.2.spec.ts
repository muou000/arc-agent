import { test, expect } from '@playwright/test';
import { editCell, expectCell, openWorkbook, openRowMenu, openColumnMenu } from './helpers';

// covers: REQ-2-2-1
test('REQ-2-2-1: inserting a row above shifts records and adjusts formula references', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'C1', '=B1+B2');
  await expectCell(page, 'C1', '2000');
  await openRowMenu(page, '1', 'Insert 1 row above');
  await expectCell(page, 'A2', 'East');
  await expectCell(page, 'B2', '1200');
  await expectCell(page, 'A3', 'North');
  await expectCell(page, 'B3', '800');
  await page.getByRole('gridcell', { name: 'C2', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=B2+B3');
  await expectCell(page, 'C2', '2000');
});

// covers: REQ-2-2-1
test('REQ-2-2-1: deleting a row moves later records up and persists', async ({ page }) => {
  await openWorkbook(page);
  await openRowMenu(page, '2', 'Delete row');
  await expectCell(page, 'A1', 'East');
  await expectCell(page, 'B1', '1200');
  await expect(page.getByText('North')).toHaveCount(0);
  await page.reload();
  await expectCell(page, 'A1', 'East');
  await expectCell(page, 'B1', '1200');
  await expect(page.getByText('North')).toHaveCount(0);
});

// covers: REQ-2-2-1
test('REQ-2-2-1: the row menu offers below insertion with the same shift semantics', async ({ page }) => {
  await openWorkbook(page);
  await openRowMenu(page, '1', 'Insert 1 row below');
  await expectCell(page, 'A1', 'East');
  await expectCell(page, 'A2', 'North');
  await expectCell(page, 'B2', '800');
  await expectCell(page, 'B1', '1200');
  await page.reload();
  await expectCell(page, 'A2', 'North');
});

// covers: REQ-2-2-2
test('REQ-2-2-2: inserting a column left shifts records and adjusts formula references', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'C1', '=B1*2');
  await expectCell(page, 'C1', '2400');
  await openColumnMenu(page, 'B', 'Insert 1 column left');
  await expectCell(page, 'C1', '1200');
  await page.getByRole('gridcell', { name: 'D1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=C1*2');
  await expectCell(page, 'D1', '2400');
});

// covers: REQ-2-2-2
test('REQ-2-2-2: deleting a referenced column shows #REF! and keeps other data', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'C1', '=B1*2');
  await openColumnMenu(page, 'B', 'Delete column');
  await expect(page.getByText('North')).toHaveCount(0);
  await expect(page.getByText('800')).toHaveCount(0);
  await expect(page.getByRole('gridcell', { name: 'C1', exact: true })).toContainText('#REF!');
  await expectCell(page, 'A1', 'East');
});

// covers: REQ-2-2-2
test('REQ-2-2-2: the column menu offers right insertion with the same shift semantics', async ({ page }) => {
  await openWorkbook(page);
  await openColumnMenu(page, 'A', 'Insert 1 column right');
  await expectCell(page, 'A1', 'East');
  await expectCell(page, 'C1', '1200');
  await expectCell(page, 'C2', '800');
  await page.reload();
  await expectCell(page, 'C1', '1200');
});
