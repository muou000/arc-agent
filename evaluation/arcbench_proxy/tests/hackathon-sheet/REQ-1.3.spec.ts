import { test, expect } from '@playwright/test';
import { exportCsv, importCsv, openHome, openWorkbook } from './helpers';

// covers: REQ-1-3-1
test('REQ-1-3-1: import quoted, escaped and multibyte CSV content', async ({ page }) => {
  const csv = [
    'Region,Amount,Note',
    '"East, Zone",1200,"He said ""ok"""',
    '华北,800,"line1\nline2"',
  ].join('\n');
  await importCsv(page, 'region report.csv', csv);
  const editor = page.getByRole('tab', { name: 'Sheet1', exact: true });
  await expect(editor).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('heading', { name: /region report/i })).toBeVisible();
  await expect(page.getByRole('gridcell', { name: 'A1', exact: true })).toContainText('Region');
  await expect(page.getByText('East, Zone')).toBeVisible();
  await expect(page.getByText('He said "ok"')).toBeVisible();
  await expect(page.getByText('华北')).toBeVisible();
  await expect(page.getByText('line1')).toBeVisible();
  await expect(page.getByText('1200').first()).toBeVisible();
});

// covers: REQ-1-3-1
test('REQ-1-3-1: imported rows keep their order and survive refresh', async ({ page }) => {
  const csv = 'alpha,1,beta\n,gamma,\ndelta,2,epsilon';
  await importCsv(page, 'ordered rows.csv', csv);
  await expect(page.getByRole('heading', { name: /ordered rows/i })).toBeVisible();
  const first = page.url();
  await expect(page.getByText('alpha')).toBeVisible();
  await expect(page.getByText('gamma')).toBeVisible();
  await expect(page.getByText('delta')).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(first);
  await expect(page.getByRole('heading', { name: /ordered rows/i })).toBeVisible();
  await expect(page.getByText('alpha')).toBeVisible();
  await expect(page.getByText('delta')).toBeVisible();
});

// covers: REQ-1-3-1
test('REQ-1-3-1: an unterminated quoted field is rejected without leaving a workbook', async ({ page }) => {
  await importCsv(page, 'broken file.csv', 'name,value\n"unterminated,2');
  await expect(page.getByText('Invalid CSV file format. Import failed.')).toBeVisible();
  await openHome(page);
  await expect(page.getByRole('link', { name: 'broken file', exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'broken file.csv', exact: true })).toHaveCount(0);
});

// covers: REQ-1-3-1
test('REQ-1-3-1: workbook name drops the csv extension and first row stays data', async ({ page }) => {
  await importCsv(page, 'plain data.csv', 'header like,values\nx,1');
  await expect(page.getByRole('heading', { name: /plain data/i })).toBeVisible();
  await page.getByRole('gridcell', { name: 'A1', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Formula bar', exact: true })).toHaveValue('header like');
  await page.reload();
  await expect(page.getByText('header like')).toBeVisible();
});

// covers: REQ-1-3-2
test('REQ-1-3-2: export downloads a csv of displayed values including formula results', async ({ page }) => {
  await openWorkbook(page);
  const editor = page.getByRole('textbox', { name: 'Formula bar', exact: true });
  await page.getByRole('gridcell', { name: 'D1', exact: true }).click();
  await editor.fill('42');
  await editor.press('Enter');
  await page.getByRole('gridcell', { name: 'E1', exact: true }).click();
  await editor.fill('=D1*2');
  await editor.press('Enter');
  const text = await exportCsv(page);
  const lines = text.trim().split(/\r?\n/);
  expect(lines[0]).toContain('Region');
  expect(lines[0]).toContain('42');
  expect(lines[0]).toContain('84');
  expect(text).not.toContain('=D1*2');
});

// covers: REQ-1-3-2
test('REQ-1-3-2: export escapes commas, quotes and line breaks', async ({ page }) => {
  await importCsv(page, 'escape probe.csv', 'a,b\n"x,y","he said ""hi"""\nline1\nline2,z');
  const text = await exportCsv(page);
  const lines = text.trim().split(/\r?\n/);
  expect(lines[1]).toContain('"x,y"');
  expect(lines[1]).toContain('"he said ""hi"""');
  const flat = text.replace(/\r?\n/g, '\n');
  expect(flat).toContain('line1\nline2,z');
});

// covers: REQ-1-3-2
test('REQ-1-3-2: the editor state is unchanged before and after export', async ({ page }) => {
  await openWorkbook(page);
  const before = page.url();
  await exportCsv(page);
  await expect(page).toHaveURL(before);
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('gridcell', { name: 'A1', exact: true })).toContainText('Region');
  await page.reload();
  await expect(page.getByRole('gridcell', { name: 'A1', exact: true })).toContainText('Region');
  await expect(page.getByRole('tab', { name: 'Sheet1', exact: true })).toHaveAttribute('aria-selected', 'true');
});
