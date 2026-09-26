import { test, expect } from '@playwright/test';
import { cell, editCell, expectCell, openRowMenu, openWorkbook, selectRange } from './helpers';

// covers: REQ-3-2-1
test('REQ-3-2-1: copying a range preserves its layout and leaves the source unchanged', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'B2');
  await page.getByRole('gridcell', { name: 'A1', exact: true }).press('Control+C');
  await cell(page, 'D5').click();
  await page.keyboard.press('Control+V');
  await expectCell(page, 'D5', 'East');
  await expectCell(page, 'E5', '1200');
  await expectCell(page, 'D6', 'North');
  await expectCell(page, 'E6', '800');
  await expectCell(page, 'A1', 'Region');
  await expectCell(page, 'B2', '800');
  await page.reload();
  await expectCell(page, 'D6', 'North');
  await expectCell(page, 'E6', '800');
  await expectCell(page, 'A1', 'Region');
});

// covers: REQ-3-2-1
test('REQ-3-2-1: cutting a range clears the source only after the target is complete', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'B1');
  await page.getByRole('gridcell', { name: 'A1', exact: true }).press('Control+X');
  await cell(page, 'D1').click();
  await page.keyboard.press('Control+V');
  await expectCell(page, 'D1', 'East');
  await expectCell(page, 'E1', '1200');
  await expect(page.getByRole('gridcell', { name: 'A1', exact: true })).not.toContainText('East');
  await expectCell(page, 'A2', 'North');
  await page.reload();
  await expectCell(page, 'D1', 'East');
  await expectCell(page, 'E1', '1200');
  await expect(page.getByRole('gridcell', { name: 'A1', exact: true })).not.toContainText('East');
});

// covers: REQ-3-2-2
test('REQ-3-2-2: undo restores a cell edit via button and Ctrl+Z', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'A1', 'Temporary');
  await expectCell(page, 'A1', 'Temporary');
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expectCell(page, 'A1', 'Region');
  await editCell(page, 'A1', 'Second edit');
  await page.keyboard.press('Control+Z');
  await expectCell(page, 'A1', 'Region');
});

// covers: REQ-3-2-2
test('REQ-3-2-2: consecutive undos reverse order and redo reapplies, then a new edit kills redo', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', 'first');
  await editCell(page, 'D2', 'second');
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expectCell(page, 'D1', 'first');
  await expect(page.getByRole('gridcell', { name: 'D2', exact: true })).not.toContainText('second');
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect(page.getByRole('gridcell', { name: 'D1', exact: true })).not.toContainText('first');
  await page.getByRole('button', { name: 'Redo', exact: true }).click();
  await expectCell(page, 'D1', 'first');
  await editCell(page, 'D3', 'fresh branch');
  await expect(page.getByRole('button', { name: 'Redo', exact: true })).toBeDisabled();
});

// covers: REQ-3-2-2
test('REQ-3-2-2: undo reverts a structural row insertion and persists after refresh', async ({ page }) => {
  await openWorkbook(page);
  await openRowMenu(page, '1', 'Insert 1 row above');
  await expect(page.getByRole('gridcell', { name: 'A1', exact: true })).not.toContainText('East');
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  await expectCell(page, 'A1', 'East');
  await expectCell(page, 'A2', 'North');
  await page.reload();
  await expectCell(page, 'A1', 'East');
  await expectCell(page, 'A2', 'North');
});
