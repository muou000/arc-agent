import { test, expect } from '@playwright/test';
import { editCell, expectCell, openWorkbook } from './helpers';

// covers: REQ-4-1-1
test('REQ-4-1-1: arithmetic expressions honor parentheses and operator precedence', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '=2+3*4');
  await expectCell(page, 'D1', '14');
  await editCell(page, 'D2', '=(2+3)*4');
  await expectCell(page, 'D2', '20');
  await editCell(page, 'D3', '=10/4');
  await expectCell(page, 'D3', '2.5');
  await page.reload();
  await expectCell(page, 'D1', '14');
  await expectCell(page, 'D2', '20');
});

// covers: REQ-4-1-1
test('REQ-4-1-1: aggregates skip empty cells and SUM never treats blanks as zero contributors', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '5');
  await editCell(page, 'D3', '7');
  await editCell(page, 'E1', '=SUM(D1:D5)');
  await expectCell(page, 'E1', '12');
  await editCell(page, 'E2', '=AVERAGE(D1:D5)');
  await expectCell(page, 'E2', '6');
  await editCell(page, 'E3', '=MIN(D1:D5)');
  await expectCell(page, 'E3', '5');
  await editCell(page, 'E4', '=MAX(D1:D5)');
  await expectCell(page, 'E4', '7');
});

// covers: REQ-4-1-1
test('REQ-4-1-1: COUNT counts only numeric cells and names are case-insensitive', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '4');
  await editCell(page, 'D2', 'text');
  await editCell(page, 'D3', '6');
  await editCell(page, 'E1', '=COUNT(D1:D5)');
  await expectCell(page, 'E1', '2');
  await editCell(page, 'E2', '=sum(D1:D5)');
  await expectCell(page, 'E2', '10');
  await editCell(page, 'E3', '=Sum(D1:D5)');
  await expectCell(page, 'E3', '10');
});

// covers: REQ-4-1-1
test('REQ-4-1-1: the formula bar keeps the original expression while the grid shows results', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '3');
  await editCell(page, 'E1', '=(D1+2)*2');
  await expectCell(page, 'E1', '10');
  await page.getByRole('gridcell', { name: 'E1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=(D1+2)*2');
  await page.reload();
  await expectCell(page, 'E1', '10');
  await page.getByRole('gridcell', { name: 'E1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=(D1+2)*2');
});

// covers: REQ-4-1-2
test('REQ-4-1-2: copying a formula adjusts relative references to the target offset', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '2');
  await editCell(page, 'D2', '9');
  await editCell(page, 'E1', '=D1*3');
  await expectCell(page, 'E1', '6');
  await page.getByRole('gridcell', { name: 'E1', exact: true }).press('Control+C');
  await page.getByRole('gridcell', { name: 'E2', exact: true }).press('Control+V');
  await expectCell(page, 'E2', '27');
  await page.getByRole('gridcell', { name: 'E2', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=D2*3');
  await expectCell(page, 'E1', '6');
  await page.reload();
  await expectCell(page, 'E2', '27');
});

// covers: REQ-4-1-2
test('REQ-4-1-2: absolute references stay fixed while relative ones shift', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '5');
  await editCell(page, 'D2', '3');
  await editCell(page, 'E1', '=$D$1+D2');
  await expectCell(page, 'E1', '8');
  await page.getByRole('gridcell', { name: 'E1', exact: true }).press('Control+C');
  await page.getByRole('gridcell', { name: 'F2', exact: true }).press('Control+V');
  await expectCell(page, 'F2', '8');
  await page.getByRole('gridcell', { name: 'F2', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=$D$1+D3');
});

// covers: REQ-4-1-2
test('REQ-4-1-2: an out-of-bounds relative reference shows #REF! in grid and formula bar', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'B2', '=B1+1');
  await expectCell(page, 'B2', '1201');
  await page.getByRole('gridcell', { name: 'B2', exact: true }).press('Control+C');
  await page.getByRole('gridcell', { name: 'A1', exact: true }).press('Control+V');
  await expect(page.getByRole('gridcell', { name: 'A1', exact: true })).toContainText('#REF!');
  await page.getByRole('gridcell', { name: 'A1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=#REF!');
});
