import { test, expect } from '@playwright/test';
import { editCell, expectCell, openWorkbook } from './helpers';

// covers: REQ-4-2-1
test('REQ-4-2-1: direct and indirect dependents recalculate in dependency order', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '2');
  await editCell(page, 'D2', '=D1*10');
  await editCell(page, 'D3', '=D2+1');
  await expectCell(page, 'D2', '20');
  await expectCell(page, 'D3', '21');
  await editCell(page, 'D1', '5');
  await expectCell(page, 'D2', '50');
  await expectCell(page, 'D3', '51');
  await page.reload();
  await expectCell(page, 'D3', '51');
});

// covers: REQ-4-2-1
test('REQ-4-2-1: a bulk paste refreshes every dependent formula', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '=B1*2');
  await expectCell(page, 'D1', '2400');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText('West\t99'));
  await page.getByRole('gridcell', { name: 'A1', exact: true }).click();
  await page.keyboard.press('Control+V');
  await expectCell(page, 'A1', 'West');
  await expectCell(page, 'B1', '99');
  await expectCell(page, 'D1', '198');
  await page.reload();
  await expectCell(page, 'D1', '198');
});

// covers: REQ-4-2-1
test('REQ-4-2-1: formulas outside the changed sources stay unchanged', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '11');
  await editCell(page, 'D2', '=D1+1');
  await editCell(page, 'E1', '=B1+1');
  await expectCell(page, 'D2', '12');
  await expectCell(page, 'E1', '1201');
  await editCell(page, 'D1', '100');
  await expectCell(page, 'D2', '101');
  await expectCell(page, 'E1', '1201');
  await page.reload();
  await expectCell(page, 'E1', '1201');
});

// covers: REQ-4-2-2
test('REQ-4-2-2: division by zero and unsupported names show stable error values', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '=1/0');
  await expect(page.getByRole('gridcell', { name: 'D1', exact: true })).toContainText('#DIV/0!');
  await editCell(page, 'D2', '=NOSUCHFN(1)');
  await expect(page.getByRole('gridcell', { name: 'D2', exact: true })).toContainText('#NAME?');
  await page.reload();
  await expect(page.getByRole('gridcell', { name: 'D1', exact: true })).toContainText('#DIV/0!');
  await expect(page.getByRole('gridcell', { name: 'D2', exact: true })).toContainText('#NAME?');
});

// covers: REQ-4-2-2
test('REQ-4-2-2: a malformed expression and a circular reference show their error values', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '=1+');
  await expect(page.getByRole('gridcell', { name: 'D1', exact: true })).toContainText('#ERROR!');
  await editCell(page, 'D2', '=D2');
  await expect(page.getByRole('gridcell', { name: 'D2', exact: true })).toContainText('#REF!');
  await page.getByRole('gridcell', { name: 'D1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('=1+');
});

// covers: REQ-4-2-2
test('REQ-4-2-2: an error cell does not block editing or recalculating other cells', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '=1/0');
  await expect(page.getByRole('gridcell', { name: 'D1', exact: true })).toContainText('#DIV/0!');
  await editCell(page, 'D2', '7');
  await editCell(page, 'D3', '=D2*2');
  await expectCell(page, 'D3', '14');
  await page.reload();
  await expectCell(page, 'D3', '14');
  await expect(page.getByRole('gridcell', { name: 'D1', exact: true })).toContainText('#DIV/0!');
});

// covers: REQ-4-2-2
test('REQ-4-2-2: fixing an error updates dependents and removes the error after refresh', async ({ page }) => {
  await openWorkbook(page);
  await editCell(page, 'D1', '=1/0');
  await editCell(page, 'D2', '=D1+1');
  await expect(page.getByRole('gridcell', { name: 'D2', exact: true })).toContainText('#DIV/0!');
  await editCell(page, 'D1', '=4/2');
  await expectCell(page, 'D1', '2');
  await expectCell(page, 'D2', '3');
  await page.reload();
  await expectCell(page, 'D1', '2');
  await expectCell(page, 'D2', '3');
  await expect(page.getByText('#DIV/0!')).toHaveCount(0);
});
