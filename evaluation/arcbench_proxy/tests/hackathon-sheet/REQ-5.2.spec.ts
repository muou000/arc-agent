import { test, expect } from '@playwright/test';
import {
  applyDropdownValidation,
  applyNumberValidation,
  cell,
  editCell,
  expectCell,
  openWorkbook,
  selectRange,
} from './helpers';

// covers: REQ-5-2-1
test('REQ-5-2-1: a dropdown rule offers its trimmed values as options', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'C2', 'C3');
  await applyDropdownValidation(page, ' Open , Closed , Draft ');
  const opener = page.getByRole('button', { name: 'Open dropdown for C2', exact: true });
  await expect(opener).toBeVisible();
  await opener.click();
  await expect(page.getByRole('option', { name: 'Open', exact: true })).toBeVisible();
  await expect(page.getByRole('option', { name: 'Closed', exact: true })).toBeVisible();
  await expect(page.getByRole('option', { name: 'Draft', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Open dropdown for C2', exact: true })).toBeVisible();
});

// covers: REQ-5-2-1
test('REQ-5-2-1: an out-of-range number is rejected with the rule message and keeps the old value', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'B2', 'B3');
  await applyNumberValidation(page, '1', '1000');
  await editCell(page, 'B2', '2000');
  await expect(page.getByText('Please enter a number between 1 and 1000')).toBeVisible();
  await expectCell(page, 'B2', '1200');
  await page.reload();
  await expectCell(page, 'B2', '1200');
});

// covers: REQ-5-2-1
test('REQ-5-2-1: a bulk paste with any invalid target is rejected as a whole', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'B2', 'B3');
  await applyNumberValidation(page, '0', '100');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText('50\t101'));
  await page.getByRole('gridcell', { name: 'B2', exact: true }).click();
  await page.keyboard.press('Control+V');
  await expect(page.getByText('Please enter a number from 0 to 100')).toBeVisible();
  await expectCell(page, 'B2', '1200');
  await expectCell(page, 'B3', '800');
});

// covers: REQ-5-2-1
test('REQ-5-2-1: reopening a rule prefills it and Delete rule lifts the constraint', async ({ page }) => {
  await openWorkbook(page);
  await selectRange(page, 'B2', 'B3');
  await applyNumberValidation(page, '0', '100');
  await openWorkbook(page);
  await selectRange(page, 'B2', 'B3');
  await page
    .getByRole('menuitem', { name: 'Data validation', exact: true })
    .or(page.getByRole('button', { name: 'Data validation', exact: true }))
    .first()
    .click();
  const dialog = page.getByRole('dialog', { name: 'Data validation', exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('combobox', { name: 'Rule type', exact: true })).toContainText(/number/i);
  await expect(dialog.getByLabel('Minimum', { exact: true })).toHaveValue('0');
  await expect(dialog.getByLabel('Maximum', { exact: true })).toHaveValue('100');
  await dialog.getByRole('button', { name: 'Delete rule', exact: true }).click();
  await expect(dialog).toBeHidden();
  await editCell(page, 'B2', '5000');
  await expectCell(page, 'B2', '5000');
});
