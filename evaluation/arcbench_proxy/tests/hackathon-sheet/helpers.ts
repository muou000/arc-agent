import { expect, Locator, Page } from '@playwright/test';

export const FIXTURES = {
  workbook: 'Q3 Sales',
  worksheet: 'Sheet1',
} as const;

export async function openHome(page: Page): Promise<void> {
  await page.goto('/');
}

export async function openWorkbook(page: Page, workbook = FIXTURES.workbook): Promise<void> {
  await openHome(page);
  const link = page.getByRole('link', { name: workbook, exact: true });
  await expect(link).toBeVisible();
  await link.click();
  await expect(page.getByRole('heading', { name: new RegExp(workbook, 'i') })).toBeVisible();
}

export async function createBlankWorkbook(page: Page): Promise<void> {
  await openHome(page);
  await page.getByRole('button', { name: 'New blank workbook', exact: true }).click();
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await expect(page.getByRole('tab', { name: FIXTURES.worksheet, exact: true })).toHaveAttribute('aria-selected', 'true');
}

export function cell(page: Page, address: string): Locator {
  return page.getByRole('gridcell', { name: address, exact: true });
}

export async function expectGrid(page: Page): Promise<void> {
  const grid = page.getByRole('grid', { name: 'Worksheet grid', exact: true });
  await expect(grid).toBeVisible();
  await expect(grid).toHaveAttribute('aria-multiselectable', 'true');
}

export async function editCell(page: Page, address: string, value: string): Promise<void> {
  const target = cell(page, address);
  await expect(target).toBeVisible();
  await target.click();
  const formulaBar = page.getByRole('textbox', { name: 'Formula bar', exact: true });
  await expect(formulaBar).toBeVisible();
  await formulaBar.fill(value);
  await formulaBar.press('Enter');
  await expect(formulaBar).toHaveValue(value);
}

export async function expectCell(page: Page, address: string, value: string): Promise<void> {
  await expect(cell(page, address)).toContainText(value);
}

// Opens a named menu by clicking its trigger, accepting either a button or a
// menuitem presentation for the trigger itself.
export async function openMenu(page: Page, name: string): Promise<void> {
  const trigger = page
    .getByRole('button', { name, exact: true })
    .or(page.getByRole('menuitem', { name, exact: true }));
  await trigger.first().click();
}

// Selects a rectangular range by dragging from one corner to the other.
export async function selectRange(page: Page, from: string, to: string): Promise<void> {
  const source = cell(page, from);
  const target = cell(page, to);
  const fromBox = await source.boundingBox();
  const toBox = await target.boundingBox();
  if (!fromBox || !toBox) throw new Error(`Cannot resolve bounds for ${from}:${to}`);
  await page.mouse.move(fromBox.x + fromBox.width / 2, fromBox.y + fromBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(toBox.x + toBox.width / 2, toBox.y + toBox.height / 2);
  await page.mouse.up();
}

export async function openWorksheetTabMenu(page: Page, tabName: string, itemName: string): Promise<void> {
  const tab = page.getByRole('tab', { name: tabName, exact: true });
  await tab.click({ button: 'right' });
  await page.getByRole('menuitem', { name: itemName, exact: true }).click();
}

export async function openRowMenu(page: Page, rowNumber: string, itemName: string): Promise<void> {
  const header = page
    .getByRole('rowheader', { name: rowNumber, exact: true })
    .or(page.getByRole('gridcell', { name: rowNumber, exact: true }));
  await header.first().click({ button: 'right' });
  await page.getByRole('menuitem', { name: itemName, exact: true }).click();
}

export async function openColumnMenu(page: Page, columnLetter: string, itemName: string): Promise<void> {
  const header = page
    .getByRole('columnheader', { name: columnLetter, exact: true })
    .or(page.getByRole('gridcell', { name: columnLetter, exact: true }));
  await header.first().click({ button: 'right' });
  await page.getByRole('menuitem', { name: itemName, exact: true }).click();
}

// Types the value directly into the selected cell, then commits with Enter.
export async function editCellInGrid(page: Page, address: string, value: string): Promise<void> {
  const target = cell(page, address);
  await expect(target).toBeVisible();
  await target.click();
  await page.keyboard.insertText(value);
  await page.keyboard.press('Enter');
}

// Imports a CSV from memory through the "Import CSV" dialog.
export async function importCsv(page: Page, fileName: string, content: string): Promise<void> {
  await openHome(page);
  await page.getByRole('button', { name: 'Import CSV', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Import CSV', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('CSV file', { exact: true }).setInputFiles({
    name: fileName,
    mimeType: 'text/csv',
    buffer: Buffer.from(content, 'utf-8'),
  });
  await dialog.getByRole('button', { name: 'Confirm import', exact: true }).click();
}

// Exports the active worksheet and returns the decoded CSV text.
export async function exportCsv(page: Page): Promise<string> {
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export CSV', exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.csv$/i);
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream) {
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks).toString('utf-8');
}

// Creates a filter over the seeded headered range and applies a value filter.
export async function applyValueFilter(page: Page, header: string, values: string[]): Promise<void> {
  await openMenu(page, 'Data');
  await page.getByRole('menuitem', { name: 'Create filter', exact: true }).click();
  await page.getByRole('button', { name: `Filter ${header}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: `Filter ${header}`, exact: true });
  await expect(dialog).toBeVisible();
  for (const value of values) {
    await dialog.getByRole('checkbox', { name: value, exact: true }).check();
  }
  await dialog.getByRole('button', { name: 'Apply', exact: true }).click();
}

// Applies a condition filter (e.g. "Text contains") on the given header.
export async function applyConditionFilter(page: Page, header: string, condition: string, value: string): Promise<void> {
  await openMenu(page, 'Data');
  await page.getByRole('menuitem', { name: 'Create filter', exact: true }).click();
  await page.getByRole('button', { name: `Filter ${header}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: `Filter ${header}`, exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('combobox', { name: 'Condition', exact: true }).selectOption({ label: condition });
  if (value !== '') {
    await dialog.getByLabel('Value', { exact: true }).fill(value);
  }
  await dialog.getByRole('button', { name: 'Apply', exact: true }).click();
}

// Sorts the currently selected range through the "Sort range" dialog.
export async function sortRange(page: Page, column: string, order: 'Ascending' | 'Descending'): Promise<void> {
  await openMenu(page, 'Data');
  await page.getByRole('menuitem', { name: 'Sort range', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Sort range', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('combobox', { name: 'Sort by', exact: true }).selectOption({ label: column });
  await dialog.getByRole('combobox', { name: 'Order', exact: true }).selectOption({ label: order });
  await dialog.getByRole('button', { name: 'Sort', exact: true }).click();
}

// Applies a numeric-range validation rule to the currently selected range.
export async function applyNumberValidation(page: Page, min: string, max: string): Promise<void> {
  await openMenu(page, 'Data');
  await page.getByRole('menuitem', { name: 'Data validation', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Data validation', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('combobox', { name: 'Rule type', exact: true }).selectOption({ label: 'Number range' });
  await dialog.getByLabel('Minimum', { exact: true }).fill(min);
  await dialog.getByLabel('Maximum', { exact: true }).fill(max);
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog).toBeHidden();
}

// Applies a dropdown validation rule to the currently selected range.
export async function applyDropdownValidation(page: Page, allowedValues: string): Promise<void> {
  await openMenu(page, 'Data');
  await page.getByRole('menuitem', { name: 'Data validation', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Data validation', exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('combobox', { name: 'Rule type', exact: true }).selectOption({ label: 'Dropdown' });
  await dialog.getByLabel('Allowed values', { exact: true }).fill(allowedValues);
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog).toBeHidden();
}

// Creates a pivot table from the given source range and configures the fields.
export async function createPivotTable(
  page: Page,
  sourceRange: string,
  fields: { rows?: string; columns?: string; values?: string; summarizeBy?: string },
): Promise<void> {
  await selectRange(page, sourceRange.split(':')[0], sourceRange.split(':')[1]);
  await openMenu(page, 'Data');
  await page.getByRole('menuitem', { name: 'Create pivot table', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Create pivot table', exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(`Source range: ${sourceRange}`)).toBeVisible();
  await dialog.getByRole('radio', { name: 'New worksheet', exact: true }).check();
  await dialog.getByRole('button', { name: 'Create', exact: true }).click();
  const editor = page.getByRole('region', { name: 'Pivot table editor', exact: true });
  await expect(editor).toBeVisible();
  if (fields.rows) {
    await editor.getByRole('combobox', { name: 'Rows', exact: true }).selectOption({ label: fields.rows });
  }
  if (fields.columns) {
    await editor.getByRole('combobox', { name: 'Columns', exact: true }).selectOption({ label: fields.columns });
  }
  if (fields.values) {
    await editor.getByRole('combobox', { name: 'Values', exact: true }).selectOption({ label: fields.values });
  }
  if (fields.summarizeBy) {
    await editor.getByRole('combobox', { name: 'Summarize by', exact: true }).selectOption({ label: fields.summarizeBy });
  }
  await editor.getByRole('button', { name: 'Apply', exact: true }).click();
}
