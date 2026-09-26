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
