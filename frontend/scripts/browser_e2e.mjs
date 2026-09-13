// 真实浏览器 E2E：用系统 Chrome (playwright-core) 驱动完整验收流程并截图。
// 流程：客户创建会话 → 查询政策 → 查询订单 → 发起退款 → 审批人审批 → 查询操作结果。
import { chromium } from 'playwright-core';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(__dirname, '../../evidence/screenshots');
const BASE = process.env.BASE ?? 'http://127.0.0.1:3000';
const Q = (o) => new URLSearchParams(o).toString();

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const log = [];
const shot = async (page, name) => {
  const p = path.join(OUT, name);
  await page.screenshot({ path: p, fullPage: true });
  log.push(`[shot] ${name} -> ${p}`);
};

// ---- 客户视角 ----
const cctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const cpage = await cctx.newPage();
cpage.on('response', (response) => {
  if (response.url().includes('/api/')) console.log(`[api] ${response.status()} ${response.url()}`);
});
await cpage.goto(`${BASE}/login?${Q({ tenant: 'TENANT-A', tenantName: '租户A', user: 'USER-001', role: 'customer' })}`);
await cpage.waitForURL('**/chat**', { timeout: 15000 });
await cpage.waitForTimeout(800);
await shot(cpage, 'b02_chat_customer.png');

// 新建会话
await cpage.getByRole('button', { name: /新建会话/ }).click();
await cpage.waitForTimeout(3000);

// 查询政策
const input = cpage.locator('input[placeholder^="例如："]');
const sendBtn = input.locator('xpath=following-sibling::button');
await input.fill('退货政策是什么？');
await sendBtn.click();
await cpage.waitForTimeout(2500);

// 查询订单
await input.fill('查订单 ORD-001');
await sendBtn.click();
await cpage.waitForTimeout(2500);

// 发起退款 → 等待审批
await input.fill('我要退款，订单号 ORD-001');
await sendBtn.click();
await cpage.waitForTimeout(6000);
const bodyText = await cpage.locator('body').innerText();
if (!bodyText.includes('等待审批')) {
  await shot(cpage, 'b03_customer_flow_debug.png');
  throw new Error(`退款流程未进入审批：${bodyText.slice(-1200)}`);
}
await cpage.waitForTimeout(800);
await shot(cpage, 'b03_customer_approval_required.png');
await shot(cpage, 'trace_before_approval_desktop.png');
log.push('[flow] 客户会话 -> 政策/订单查询 -> 退款触发审批完成');

// ---- 审批人视角 ----
const actx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const apage = await actx.newPage();
await apage.goto(`${BASE}/login?${Q({ tenant: 'TENANT-A', tenantName: '租户A', user: 'APPROVER-A', role: 'approver' })}`);
await apage.waitForURL('**/approvals**', { timeout: 15000 });
await apage.waitForTimeout(1000);
await shot(apage, 'b04_approvals_pending.png');

// 点击待审批项（列表第一项）
const firstItem = apage.locator('.ant-list-item').first();
await firstItem.click();
await apage.waitForTimeout(600);

// 点击“通过” → 二次确认弹窗
await apage.getByRole('button', { name: /通\s*过/ }).first().click();
await apage.waitForSelector('text=确认通过审批', { timeout: 5000 });
await shot(apage, 'b05_approvals_confirm_modal.png');
await apage.getByRole('button', { name: /确认通过/ }).click();
await apage.waitForTimeout(2000);
await shot(apage, 'b06_approvals_approved.png');
log.push('[flow] 审批人 -> 二次确认 -> 审批通过完成');

// 客户页保持对原 trace_id 的只读订阅；审批后 Shadow 生命周期必须合并回原消息。
await cpage.bringToFront();
await cpage.waitForFunction(() => {
  const text = document.body.innerText;
  return text.includes('Shadow 执行') && text.includes('执行完成') && text.includes('审批通过');
}, { timeout: 30000 });
await cpage.waitForTimeout(500);
await shot(cpage, 'trace_after_shadow_desktop.png');

await cpage.setViewportSize({ width: 390, height: 844 });
await cpage.waitForTimeout(500);
await shot(cpage, 'trace_after_shadow_mobile.png');
log.push('[flow] 原 trace_id 订阅 -> Shadow 开始/完成事件合并并完成桌面/移动截图');

await browser.close();
console.log(log.join('\n'));
console.log('BROWSER_E2E_OK');
