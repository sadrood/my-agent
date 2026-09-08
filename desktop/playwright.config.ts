import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:8096',
    headless: true,
    // 免下载：用系统已装 Chrome（chromium-1243 官方源下载过慢时的替代）
    channel: 'msedge',  // 系统自装 Chrome 路径非默认，改用系统 Edge
  },
  // 目标是已运行的 web demo（8096 静态 + 8090 后端），不由测试启动服务
  webServer: undefined,
  reporter: [['list']],
  retries: 0,
});
