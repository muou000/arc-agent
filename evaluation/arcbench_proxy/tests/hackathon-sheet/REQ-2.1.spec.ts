import { test, expect } from '@playwright/test';
import {
  cell,
  createBlankWorkbook,
  editCell,
  expectCell,
  openWorkbook,
  openWorksheetTabMenu,
} from './helpers';

// covers: REQ-2-1-1
test('REQ-2-1-1: add a worksheet using the first unused name', async ({ page }) => {
  await openWorkbook(page);
  await expect(page.getByRole('tab', { name: 'Sheet2', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Add worksheet', exact: true }).click();
  const added = page.getByRole('tab', { name: 'Sheet3', exact: true });
  await expect(added).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('East')).toHaveCount(0);
});

// covers: REQ-2-1-1
test('REQ-2-1-1: the added worksheet survives a refresh', async ({ page }) => {
  await openWorkbook(page);
  await page.getByRole('button', { name: 'Add worksheet', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Sheet3', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Sheet2', exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Sheet3', exact: true })).toBeVisible();
});

// covers: REQ-2-1-1
test('REQ-2-1-1: adding a worksheet leaves existing data untouched', async ({ page }) => {
  await openWorkbook(page);
  await page.getByRole('button', { name: 'Add worksheet', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Sheet3', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Sheet2', exact: true }).click();
  await expectCell(page, 'A1', 'East');
  await expectCell(page, 'B1', '1200');
  await expectCell(page, 'A2', 'North');
  await expectCell(page, 'B2', '800');
});

// covers: REQ-2-1-2
test('REQ-2-1-2: switching worksheets swaps grid state and restores the source', async ({ page }) => {
  await openWorkbook(page);
  await expectCell(page, 'A1', 'Region');
  await page.getByRole('tab', { name: 'Sheet2', exact: true }).click();
  await expectCell(page, 'A1', 'East');
  await expectCell(page, 'B1', '1200');
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Sheet1', exact: true }).click();
  await expectCell(page, 'A1', 'Region');
  await expect(page.getByText('1200')).toHaveCount(0);
});

// covers: REQ-2-1-2
test('REQ-2-1-2: a worksheet opened without history selects A1', async ({ page }) => {
  await openWorkbook(page);
  await page.getByRole('tab', { name: 'Sheet2', exact: true }).click();
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue(/East|$/);
});

// covers: REQ-2-1-2
test('REQ-2-1-2: reopening shows the last active tab and saved cell per sheet', async ({ page }) => {
  await openWorkbook(page);
  await page.getByRole('tab', { name: 'Sheet2', exact: true }).click();
  await cell(page, 'B2').click();
  await page.getByRole('tab', { name: 'Sheet1', exact: true }).click();
  await cell(page, 'B1').click();
  await page.reload();
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'B1')).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('tab', { name: 'Sheet2', exact: true }).click();
  await expect(cell(page, 'B2')).toHaveAttribute('aria-selected', 'true');
});

// covers: REQ-2-1-3
test('REQ-2-1-3: rename a worksheet from the tab menu', async ({ page }) => {
  await openWorkbook(page);
  await openWorksheetTabMenu(page, 'Sheet2', 'Rename');
  const dialog = page.getByRole('dialog', { name: 'Rename worksheet', exact: true });
  await expect(dialog.getByLabel('Worksheet name', { exact: true })).toHaveValue('Sheet2');
  await dialog.getByLabel('Worksheet name', { exact: true }).fill('Archive');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Archive', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('tab', { name: 'Archive', exact: true })).toBeVisible();
});

// covers: REQ-2-1-3
test('REQ-2-1-3: an empty worksheet name is rejected', async ({ page }) => {
  await openWorkbook(page);
  await openWorksheetTabMenu(page, 'Sheet2', 'Rename');
  const dialog = page.getByRole('dialog', { name: 'Rename worksheet', exact: true });
  await dialog.getByLabel('Worksheet name', { exact: true }).fill('   ');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Worksheet name cannot be empty', { exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Sheet2', exact: true })).toBeVisible();
});

// covers: REQ-2-1-3
test('REQ-2-1-3: a duplicate worksheet name is rejected', async ({ page }) => {
  await openWorkbook(page);
  await openWorksheetTabMenu(page, 'Sheet2', 'Rename');
  const dialog = page.getByRole('dialog', { name: 'Rename worksheet', exact: true });
  await dialog.getByLabel('Worksheet name', { exact: true }).fill('Sheet1');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Worksheet name already exists', { exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Sheet2', exact: true })).toBeVisible();
});

// covers: REQ-2-1-4
test('REQ-2-1-4: delete a worksheet after confirming the target', async ({ page }) => {
  await openWorkbook(page);
  await openWorksheetTabMenu(page, 'Sheet2', 'Delete');
  const dialog = page.getByRole('dialog', { name: 'Delete worksheet', exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText('Sheet2')).toBeVisible();
  await dialog.getByRole('button', { name: 'Delete worksheet', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Sheet2', exact: true })).toHaveCount(0);
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('tab', { name: 'Sheet2', exact: true })).toHaveCount(0);
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toBeVisible();
});

// covers: REQ-2-1-4
test('REQ-2-1-4: the last remaining worksheet cannot be deleted', async ({ page }) => {
  await createBlankWorkbook(page);
  await openWorksheetTabMenu(page, 'Sheet1', 'Delete');
  await expect(page.getByRole('dialog', { name: 'Delete worksheet', exact: true })).toHaveCount(0);
  await expect(page.getByText('A workbook must contain at least one worksheet')).toBeVisible();
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toBeVisible();
});

// covers: REQ-2-1-4
test('REQ-2-1-4: deleting one worksheet keeps the other intact', async ({ page }) => {
  await openWorkbook(page);
  await openWorksheetTabMenu(page, 'Sheet2', 'Delete');
  const dialog = page.getByRole('dialog', { name: 'Delete worksheet', exact: true });
  await dialog.getByRole('button', { name: 'Delete worksheet', exact: true }).click();
  await expect(page.getByRole('tab', { name: 'Sheet2', exact: true })).toHaveCount(0);
  await expectCell(page, 'A1', 'Region');
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toBeVisible();
});
