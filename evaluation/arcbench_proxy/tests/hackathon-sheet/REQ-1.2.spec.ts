import { test, expect } from '@playwright/test';
import { cell, createBlankWorkbook, expectGrid, FIXTURES, openWorkbook } from './helpers';

// covers: REQ-1-2-1
test('REQ-1-2-1: create and restore a blank workbook', async ({ page }) => {
  await createBlankWorkbook(page);
  await expectGrid(page);
  const tab = page.getByRole('tab', { name: FIXTURES.worksheet, exact: true });
  await expect(tab).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
  const editorUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(editorUrl);
  await expect(tab).toHaveAttribute('aria-selected', 'true');
  await expect(cell(page, 'A1')).toHaveAttribute('aria-selected', 'true');
});

// covers: REQ-1-2-1
test('REQ-1-2-1: a created workbook is listed on the home page and reopens identically', async ({ page }) => {
  await createBlankWorkbook(page);
  await page.getByRole('button', { name: 'Rename workbook', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Workbook name', { exact: true }).fill('Home Probe');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('heading', { name: /home probe/i })).toBeVisible();
  await page.getByRole('link', { name: /^Workbook home$|^Home$/ }).first().click();
  await page.getByRole('link', { name: 'Home Probe', exact: true }).click();
  await expectGrid(page);
  await expect(page.getByRole('tab', { name: FIXTURES.worksheet, exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText(/Last updated: .+/).first()).toBeVisible();
});

// covers: REQ-1-2-2
test('REQ-1-2-2: rename a workbook and see the new name on the home page', async ({ page }) => {
  await createBlankWorkbook(page);
  await page.getByRole('button', { name: 'Rename workbook', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Workbook name', { exact: true })).toBeVisible();
  await dialog.getByLabel('Workbook name', { exact: true }).fill('Renamed Probe');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('heading', { name: /renamed probe/i })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: /renamed probe/i })).toBeVisible();
  await page.getByRole('link', { name: /^Workbook home$|^Home$/ }).first().click();
  await expect(page.getByRole('link', { name: 'Renamed Probe', exact: true })).toBeVisible();
});

// covers: REQ-1-2-2
test('REQ-1-2-2: an empty workbook name is rejected with its message', async ({ page }) => {
  await createBlankWorkbook(page);
  await page.getByRole('button', { name: 'Rename workbook', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Workbook name', { exact: true }).fill('   ');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('Workbook name cannot be empty', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: /.+/ })).toBeVisible();
  await expectGrid(page);
});

// covers: REQ-1-2-2
test('REQ-1-2-2: rename dialog prefills the last saved name and reopens with it', async ({ page }) => {
  await createBlankWorkbook(page);
  await page.getByRole('button', { name: 'Rename workbook', exact: true }).click();
  let dialog = page.getByRole('dialog');
  await dialog.getByLabel('Workbook name', { exact: true }).fill('Prefill Probe');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('heading', { name: /prefill probe/i })).toBeVisible();
  await page.getByRole('button', { name: 'Rename workbook', exact: true }).click();
  dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('Workbook name', { exact: true })).toHaveValue('Prefill Probe');
  await page.getByRole('button', { name: 'Cancel', exact: true }).or(page.getByRole('button', { name: 'Close', exact: true })).first().click();
  await openWorkbook(page, 'Prefill Probe');
  await expect(page.getByRole('heading', { name: /prefill probe/i })).toBeVisible();
});
