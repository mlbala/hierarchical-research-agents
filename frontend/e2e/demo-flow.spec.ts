import { readFile } from 'node:fs/promises'

import { expect, test } from '@playwright/test'

test('demo research flow: question to cited report and Markdown download', async ({ page }) => {
  const dialogs: string[] = []
  page.on('dialog', async (dialog) => {
    dialogs.push(dialog.message())
    await dialog.dismiss()
  })
  const projectName = `E2E mobility ${Date.now()}`

  await page.goto('/')
  await expect(page.getByText('Demo mode').first()).toBeVisible()

  // Create a project.
  await page.getByRole('button', { name: 'New project' }).first().click()
  await page.getByLabel('Name').fill(projectName)
  await page.getByRole('button', { name: 'Create project' }).click()
  await expect(page.getByRole('heading', { name: projectName })).toBeVisible()

  // Client-side validation blocks a too-short question.
  await page.getByLabel('Research question').fill('Too short')
  await page.getByRole('button', { name: 'Start research' }).click()
  await expect(page.getByText('at least 10 characters')).toBeVisible()

  // Start a quick research run.
  await page
    .getByLabel('Research question')
    .fill('Do e-bike voucher programmes reduce car trips in mid-sized cities?')
  await page.getByRole('radio', { name: 'Quick' }).click()
  await page.getByRole('button', { name: 'Start research' }).click()
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+$/)

  // Live progress: the hierarchy is shown and the run completes.
  await expect(page.getByRole('group', { name: 'Agent hierarchy' })).toBeVisible()
  await expect(page.getByText('Completed', { exact: true }).first()).toBeVisible({ timeout: 60_000 })

  // The cited report.
  await page.getByRole('tab', { name: 'Report' }).click()
  for (const heading of ['Direct answer', 'Key findings', 'Detailed analysis']) {
    await expect(page.getByRole('heading', { name: heading })).toBeVisible()
  }
  await expect(page.getByRole('heading', { name: 'Limitations and unresolved questions' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Sources', exact: true })).toBeVisible()
  await expect(page.getByText('Demo report', { exact: true })).toBeVisible()

  // Citation markers open an evidence preview.
  await page.getByRole('button', { name: /^Citation 1:/ }).first().click()
  await expect(page.getByRole('dialog').getByText('Demo fixture')).toBeVisible()
  await page.keyboard.press('Escape')

  // Download the Markdown report.
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Download .md' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/\.md$/)
  const markdown = await readFile((await download.path())!, 'utf8')
  expect(markdown).toContain('## Executive summary')
  expect(markdown).toContain('## Sources')
  expect(markdown).toContain('Demo mode')
  expect(markdown).not.toContain('<script')

  // Evidence and sources are inspectable; the injection page is shown as text only.
  await page.getByRole('tab', { name: /Sources/ }).click()
  await expect(page.getByText('prompt-injection test page')).toBeVisible()

  // Everything survives a reload (state comes from PostgreSQL, not the browser).
  await page.reload()
  await page.getByRole('tab', { name: 'Report' }).click()
  await expect(page.getByRole('heading', { name: 'Key findings' })).toBeVisible()

  // Saved-report search finds it.
  await page.getByRole('link', { name: 'Search reports' }).first().click()
  await page.getByRole('searchbox', { name: 'Search saved reports' }).fill('voucher')
  await expect(page.getByText(/report(s)? found/)).toBeVisible()

  // Untrusted page content never executed a script.
  expect(dialogs).toEqual([])
})

test('a finished run can be re-run as a new run', async ({ page }) => {
  await page.goto('/')
  const recent = page.getByRole('navigation', { name: 'Recent research' }).getByRole('link').first()
  await recent.click()
  await expect(page.getByText('Completed', { exact: true }).first()).toBeVisible({ timeout: 60_000 })
  const firstUrl = page.url()
  await page.getByRole('button', { name: 'Re-run' }).click()
  await expect(page).not.toHaveURL(firstUrl)
  // The new run starts from the queue and shows its own live progress.
  await expect(page.getByText(/^(Queued|Running)$/).first()).toBeVisible()
  await expect(page.getByText('Completed', { exact: true }).first()).toBeVisible({ timeout: 60_000 })
})
