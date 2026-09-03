"use strict";
// 消息内容提取工具：统一处理 str / content-blocks dict 数组。
export function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((b) => (typeof b === "string" ? b : (b?.text ?? "")))
      .join("");
  }
  if (content && typeof content === "object") {
    const c = content as { text?: unknown; content?: unknown };
    if (typeof c.text === "string") return c.text;
    if (typeof c.content === "string") return c.content;
  }
  return content ? String(content) : "";
}
