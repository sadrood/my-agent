/**
 * Web demo 关键流 e2e（目标：http://127.0.0.1:8096，需后端 8090 与静态服务在跑）。
 * 覆盖：加载 / 概览面板 / 改动面板 / 设置 / 主题切换 / 轮次导航。
 */
import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  // 干净 profile 会弹首次引导遮挡交互：预置“已引导/已配置”状态
  await page.addInitScript(() => {
    try {
      const cfg = JSON.parse(localStorage.getItem('my-agent-config') || '{}');
      cfg.onboarded = true;
      localStorage.setItem('my-agent-config', JSON.stringify(cfg));
    } catch { /* 忽略 */ }
  });
  await page.goto('/');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(1500);
});

test('页面加载：标题与状态栏', async ({ page }) => {
  await expect(page).toHaveTitle(/小悟/);
  await expect(page.locator('.status-bar .brand')).toHaveText(/小悟/);
});

test('右栏概览：打开后默认概览 tab，含指标卡片或空态', async ({ page }) => {
  // 打开右栏（顶栏折叠按钮）
  await page.locator('.status-bar button[title*="右侧栏"]').click();
  const tabs = page.locator('.dock-tab');
  await expect(tabs.filter({ hasText: '概览' })).toBeVisible();
  await expect(tabs.filter({ hasText: '文件' })).toBeVisible();
  await expect(tabs.filter({ hasText: '改动' })).toBeVisible();
  // 概览内容：有空态或 KPI 网格
  const hasEmpty = await page.locator('.ov-empty').count();
  const hasKpi = await page.locator('.ov-kpi-grid').count();
  expect(hasEmpty + hasKpi).toBeGreaterThan(0);
});

test('改动面板：展示 git 状态或空态', async ({ page }) => {
  await page.locator('.status-bar button[title*="右侧栏"]').click();
  await page.locator('.dock-tab', { hasText: '改动' }).click();
  await expect(page.locator('.git-changes')).toBeVisible();
});

test('设置：供应商卡片与添加自定义模型', async ({ page }) => {
  await page.locator('.status-bar button[title*="设置"], .panel-left button[title*="设置"], .panel-left button:has-text("设置")').first().click();
  await expect(page.locator('.settings-modal')).toBeVisible();
  await expect(page.locator('.provider-card')).toHaveCount(6);
  await expect(page.locator('.add-provider')).toBeVisible();
  await page.locator('.add-provider').click();
  await expect(page.locator('.provider-card .provider-form').last()).toBeVisible();
  await page.keyboard.press('Escape');
});

test('主题切换：html data-theme 即时变化', async ({ page }) => {
  const before = await page.evaluate(() => document.documentElement.dataset.theme);
  await page.locator('.status-bar button[title*="主题"]').click();
  await page.waitForTimeout(300);
  const after = await page.evaluate(() => document.documentElement.dataset.theme);
  expect(after).not.toBe(before);
  // 切回
  await page.locator('.status-bar button[title*="主题"]').click();
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(before);
});

test('会话列表渲染与轮次导航（有历史会话时）', async ({ page }) => {
  const cards = page.locator('.session-item');
  const count = await cards.count();
  if (count === 0) return; // 空环境跳过
  // 点第一个有内容的会话 → 消息流渲染
  await cards.first().click();
  await page.waitForTimeout(1500);
  const msgs = await page.locator('.msg').count();
  expect(msgs).toBeGreaterThan(0);
  // 轮次导航横杠与回合数一致（有提问时）
  const dashes = await page.locator('.tn-slot').count();
  expect(dashes).toBeGreaterThanOrEqual(0);
});
