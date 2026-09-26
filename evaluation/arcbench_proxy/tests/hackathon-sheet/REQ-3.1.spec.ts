import { test, expect } from '@playwright/test';
import { cell, editCell, editCellInGrid, expectCell, openWorkbook, selectRange } from './helpers';

// covers: REQ-3-1-1
test('REQ-3-1-1: escape cancels an uncommitted grid edit', async ({ page }) => {
  await openWorkbook(page);
  await cell(page, 'B1').click();
  await page.keyboard.insertText('999');
  await page.keyboard.press('Escape');
  await expectCell(page, 'B1', '1200');
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('1200');
  await page.reload();
  await expectCell(page, 'B1', '1200');
});

// covers: REQ-3-1-1
test('REQ-3-1-1: clicking another cell commits the grid edit', async ({ page }) => {
  await openWorkbook(page);
  await cell(page, 'C1').click();
  await page.keyboard.insertText('committed text');
  await cell(page, 'A1').click();
  await expectCell(page, 'C1', 'committed text');
  await page.getByRole('gridcell', { name: 'C1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('committed text');
  await page.reload();
  await expectCell(page, 'C1', 'committed text');
});

// covers: REQ-3-1-1
test('REQ-3-1-1: dependent formulas update after the source value changes', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'C1', '=B1+B2');
  await expectCell(page, 'C1', '2000');
  await editCell(page, 'B1', '100');
  await expectCell(page, 'C1', '900');
  await editCell(page, 'B2', '300');
  await expectCell(page, 'C1', '400');
  await page.reload();
  await expectCell(page, 'C1', '400');
  await page.getByRole('gridcell', { name: 'C1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=B1+B2');
});

// covers: REQ-3-1-2
test('REQ-3-1-2: pasting a rectangle fills every cell without spilling', async ({ page }) => {
  await openWorkbook(page);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText('Pen\t4\nRuler\t7'));
  await cell(page, 'D1').click();
  await page.keyboard.press('Control+V');
  await expectCell(page, 'D1', 'Pen');
  await expectCell(page, 'E1', '4');
  await expectCell(page, 'D2', 'Ruler');
  await expectCell(page, 'E2', '7');
  await expectCell(page, 'A1', 'Region');
  await page.reload();
  await expectCell(page, 'D1', 'Pen');
  await expectCell(page, 'E2', '7');
});

// covers: REQ-3-1-2
test('REQ-3-1-2: the grid context menu offers Paste for the same clipboard', async ({ page }) => {
  await openWorkbook(page);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText('Only\tOne'));
  await cell(page, 'D1').click({ button: 'right' });
  await page.getByRole('menuitem', { name: 'Paste', exact: true }).click();
  await expectCell(page, 'D1', 'Only');
  await expectCell(page, 'E1', 'One');
  await expect(cell(page, 'D2')).not.toContainText('Only');
});

// covers: REQ-3-1-2
test('REQ-3-1-2: pasting over formulas replaces them inside the target', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '=B1*2');
  await expectCell(page, 'D1', '2400');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText('literal\t42'));
  await cell(page, 'D1').click();
  await page.keyboard.press('Control+V');
  await expectCell(page, 'D1', 'literal');
  await expectCell(page, 'E1', '42');
  await page.getByRole('gridcell', { name: 'D1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('literal');
});

// covers: REQ-3-1-3
test('REQ-3-1-3: dragging selects exactly the rectangle in ARIA state', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'B2');
  const grid = page.getByRole('grid', { name: 'Worksheet grid', exact: true });
  await expect(grid).toHaveAttribute('aria-multiselectable', 'true');
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'B1')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'A2')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'B2')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'C1')).toHaveAttribute('aria-selected', 'false');
  await expect(cell(page, 'C3')).toHaveAttribute('aria-selected', 'false');
});

// covers: REQ-3-1-3
test('REQ-3-1-3: selecting a new cell replaces the previous rectangle', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'B2');
  await expect(cell(page, 'A2')).toHaveAttribute('aria-selected', 'true');
  await selectRange(page, 'C2', 'D3');
  await expect(cell(page, 'C2')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'D3')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'false');
  await expect(cell(page, 'B2')).toHaveAttribute('aria-selected', 'false');
});

// covers: REQ-3-1-3
test('REQ-3-1-3: the full rectangle persists across refresh, not just its corner', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'A1', 'B2');
  await page.reload();
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'B1')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'A2')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'B2')).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'C1')).toHaveAttribute('aria-selected', 'false');
  await expect(cell(page, 'D4')).toHaveAttribute('aria-selected', 'false');
});
