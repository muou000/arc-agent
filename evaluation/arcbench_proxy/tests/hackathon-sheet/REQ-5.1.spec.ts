import { test, expect } from '@playwright/test';
import { applyConditionFilter, applyValueFilter, editCell, expectCell, openWorkbook, selectRange, sortRange } from './helpers';

// covers: REQ-5-1-1
test('REQ-5-1-1: ascending sort by Region moves whole records and keeps the header', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'C6');
  await sortRange(page, 'Region', 'Ascending');
  await expectCell(page, 'A2', 'East');
  await expectCell(page, 'B2', '1200');
  await expectCell(page, 'A3', 'North');
  await expectCell(page, 'B3', '800');
  await expectCell(page, 'A4', 'South');
  await expectCell(page, 'B4', '700');
  await expectCell(page, 'A1', 'Region');
  await expectCell(page, 'C1', 'Status');
});

// covers: REQ-5-1-1
test('REQ-5-1-1: descending sort by Sales orders numeric values by type', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'C6');
  await sortRange(page, 'Sales', 'Descending');
  await expectCell(page, 'B2', '1200');
  await expectCell(page, 'A2', 'East');
  await expectCell(page, 'A3', 'North');
  await expectCell(page, 'B3', '800');
  await expectCell(page, 'A4', 'South');
  await expectCell(page, 'B4', '700');
  await page.reload();
  await expectCell(page, 'B2', '1200');
  await expectCell(page, 'A4', 'South');
});

// covers: REQ-5-1-1
test('REQ-5-1-1: sorting leaves data outside the selection untouched', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'E1', 'anchor');
  await selectRange(page, 'A1', 'C6');
  await sortRange(page, 'Region', 'Descending');
  await expectCell(page, 'E1', 'anchor');
  await expectCell(page, 'A2', 'South');
  await page.reload();
  await expectCell(page, 'E1', 'anchor');
});

// covers: REQ-5-1-2
test('REQ-5-1-2: a value filter hides only the nonmatching rows', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'C6');
  await applyValueFilter(page, 'Status', ['Open']);
  await expectCell(page, 'A2', 'East');
  await expect(page.getByText('North')).toHaveCount(0);
  await expectCell(page, 'A3', 'South');
  await page.reload();
  await expect(page.getByText('North')).toHaveCount(0);
  await expectCell(page, 'A2', 'East');
});

// covers: REQ-5-1-2
test('REQ-5-1-2: a numeric condition filter uses the Value textbox', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'C6');
  await applyConditionFilter(page, 'Sales', 'Greater than', '750');
  await expectCell(page, 'A2', 'East');
  await expectCell(page, 'A3', 'North');
  await expect(page.getByText('South')).toHaveCount(0);
});

// covers: REQ-5-1-2
test('REQ-5-1-2: conditions on different columns combine with AND', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'C6');
  await applyConditionFilter(page, 'Sales', 'Greater than', '750');
  await applyConditionFilter(page, 'Region', 'Text contains', 'ast');
  await expectCell(page, 'A2', 'East');
  await expect(page.getByText('North')).toHaveCount(0);
  await expect(page.getByText('South')).toHaveCount(0);
  await page.reload();
  await expectCell(page, 'A2', 'East');
  await expect(page.getByText('North')).toHaveCount(0);
});

// covers: REQ-5-1-2
test('REQ-5-1-2: clearing the filter restores every source row in original order', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'C6');
  await applyValueFilter(page, 'Status', ['Open']);
  await expect(page.getByText('North')).toHaveCount(0);
  await page.getByRole('button', { name: 'Clear filter', exact: true }).click();
  await expectCell(page, 'A2', 'East');
  await expectCell(page, 'A3', 'North');
  await expectCell(page, 'B3', '800');
  await expectCell(page, 'A4', 'South');
  await page.reload();
  await expectCell(page, 'A3', 'North');
  await expectCell(page, 'B3', '800');
});
