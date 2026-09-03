// 角色页面截图（真实浏览器 + 自动登录 + 同上下文跳转）。
import { chromium } from 'playwright-core';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(__dirname, '../../evidence/screenshots');
const BASE = process.env.BASE ?? 'http://127.0.0.1:3100';
const Q = (o) => new URLSearchParams(o).toString();

const browser = await chromium.launch({ channel: 'chrome', headless: true });

async function loginAs(opts) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(`${BASE}/login?${Q(opts)}`);
  await page.waitForURL((u) => u.pathname !== '/login', { timeout: 15000 });
  await page.waitForTimeout(1500);
  return { ctx, page };
}

const shot = async (page, name) => {
  await page.screenshot({ path: path.join(OUT, name), fullPage: true });
  console.log(`[shot] ${name}`);
};

// 登录页
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  await p.goto(`${BASE}/login`);
  await p.waitForTimeout(1500);
  await shot(p, '01_login.png');
  await ctx.close();
}

// admin：工作台 / 会话历史 / 成员设置
{
  const { ctx, page } = await loginAs({ tenant: 'TENANT-A', tenantName: '租户A', user: 'ADMIN-A', role: 'admin' });
  await shot(page, '07_workbench_admin.png');
  await page.goto(`${BASE}/sessions`);
  await page.waitForTimeout(1500);
  await shot(page, '08_sessions_admin.png');
  await page.goto(`${BASE}/settings`);
  await page.waitForTimeout(1500);
  await shot(page, '09_settings_admin.png');
  await ctx.close();
}

// agent：工作台（只读审批入口）
{
  const { ctx, page } = await loginAs({ tenant: 'TENANT-A', tenantName: '租户A', user: 'AGENT-A', role: 'agent' });
  await shot(page, '10_workbench_agent.png');
  await page.goto(`${BASE}/approvals`);
  await page.waitForTimeout(1500);
  await shot(page, '11_approvals_agent_readonly.png');
  await ctx.close();
}

await browser.close();
console.log('CAPTURE_OK');
