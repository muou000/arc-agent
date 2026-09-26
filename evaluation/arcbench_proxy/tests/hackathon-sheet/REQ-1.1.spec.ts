import { test, expect } from '@playwright/test';
import { createBlankWorkbook, editCell, expectCell, expectGrid, FIXTURES, openWorkbook } from './helpers';

// covers: REQ-1-1-1
test('REQ-1-1-1: open and restore a seeded workbook', async ({ page }) => {
  await openWorkbook(page);
  await expectGrid(page);
  await expect(page.getByRole('tab', { name: FIXTURES.worksheet, exact: true })).toHaveAttribute('aria-selected', 'true');
  await expectCell(page, 'A1', 'Region');
  const editorUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(editorUrl);
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.workbook, 'i') })).toBeVisible();
  await expectCell(page, 'A1', 'Region');
});

// covers: REQ-1-1-1
test('REQ-1-1-1: editor repeats the home-page last-updated value', async ({ page }) => {
  await createBlankWorkbook(page);
  await page.getByRole('button', { name: 'Rename workbook', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Workbook name', { exact: true }).fill('Stamp Probe');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('heading', { name: /stamp probe/i })).toBeVisible();
  await page.getByRole('link', { name: /^Workbook home$|^Home$/ }).first().click();
  const record = page.getByRole('link', { name: 'Stamp Probe', exact: true });
  await expect(record).toBeVisible();
  const homeStamp = await page.getByText(/Last updated: .+/).first().textContent();
  expect(homeStamp).toBeTruthy();
  await record.click();
  await expect(page.getByRole('heading', { name: /stamp probe/i })).toBeVisible();
  await expect(page.getByText(/Last updated: .+/).first()).toHaveText(homeStamp!);
});

// covers: REQ-1-1-1
test('REQ-1-1-1: another workbook never leaks data into the current grid', async ({ page }) => {
  await openWorkbook(page);
  const seededUrl = page.url();
  await expectCell(page, 'A1', 'Region');
  await createBlankWorkbook(page);
  await editCell(page, 'A1', 'Second workbook');
  await page.goto(seededUrl);
  await expect(page.getByRole('heading', { name: new RegExp(FIXTURES.workbook, 'i') })).toBeVisible();
  await expectGrid(page);
  await expectCell(page, 'A1', 'Region');
  await expect(page.getByText('Second workbook')).toHaveCount(0);
  await expect(page.getByRole('gridcell', { name: 'Pen', exact: true })).toHaveCount(0);
});
