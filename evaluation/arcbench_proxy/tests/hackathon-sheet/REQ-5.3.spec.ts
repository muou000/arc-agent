import { test } from '@playwright/test';
import { createPivotTable, editCell, expectCell, openWorkbook } from './helpers';

// covers: REQ-5-3-1
test('REQ-5-3-1: a pivot without a column field groups rows with a Grand Total', async ({ page }) => {
  await openWorkbook(page);
  await createPivotTable(page, 'A1:C6', { rows: 'Region', values: 'Sales', summarizeBy: 'SUM' });
  await expectCell(page, 'A1', 'Region');
  await expectCell(page, 'B1', 'SUM of Sales');
  await expectCell(page, 'A2', 'East');
  await expectCell(page, 'B2', '1200');
  await expectCell(page, 'A3', 'North');
  await expectCell(page, 'B3', '800');
  await expectCell(page, 'A4', 'South');
  await expectCell(page, 'B4', '700');
  await expectCell(page, 'A5', 'Grand Total');
  await expectCell(page, 'B5', '2700');
});

// covers: REQ-5-3-1
test('REQ-5-3-1: a column field arranges values from B1 with Grand Total edges', async ({ page }) => {
  await openWorkbook(page);
  await createPivotTable(page, 'A1:C6', {
    rows: 'Region',
    columns: 'Status',
    values: 'Sales',
    summarizeBy: 'SUM',
  });
  await expectCell(page, 'A1', 'Region');
  await expectCell(page, 'B1', 'Open');
  await expectCell(page, 'C1', 'Closed');
  await expectCell(page, 'A2', 'East');
  await expectCell(page, 'B2', '1200');
  await expectCell(page, 'C2', '0');
  await expectCell(page, 'A3', 'North');
  await expectCell(page, 'C3', '800');
  await expectCell(page, 'A5', 'Grand Total');
  await expectCell(page, 'B5', '1900');
  await expectCell(page, 'C5', '800');
});

// covers: REQ-5-3-1
test('REQ-5-3-1: COUNT reports 0 for combinations without records', async ({ page }) => {
  await openWorkbook(page);
  await createPivotTable(page, 'A1:C6', {
    rows: 'Region',
    columns: 'Status',
    values: 'Sales',
    summarizeBy: 'COUNT',
  });
  await expectCell(page, 'B1', 'COUNT of Sales');
  await expectCell(page, 'C2', '0');
  await expectCell(page, 'B2', '1');
  await expectCell(page, 'C3', '1');
});

// covers: REQ-5-3-1
test('REQ-5-3-1: refresh recomputes the summary from the current source values', async ({ page }) => {
  await openWorkbook(page);
  await createPivotTable(page, 'A1:C6', { rows: 'Region', values: 'Sales', summarizeBy: 'SUM' });
  await expectCell(page, 'B2', '1200');
  await page.getByRole('tab', { name: 'Sheet1', exact: true }).click();
  await editCell(page, 'B2', '500');
  await page.getByRole('button', { name: 'Refresh pivot table', exact: true }).click();
  await expectCell(page, 'B2', '500');
  await expectCell(page, 'B5', '2000');
  await page.reload();
  await expectCell(page, 'B5', '2000');
});
