import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, ".") },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    /**
     * **测试时区钉死在 America/Los_Angeles，绝不能是 Asia/Shanghai。**
     *
     * P11 那个"账号时区 19:00 的稿画在 04:00"的缺陷，带着 49 vitest + 41 playwright
     * 全绿溜进了生产，唯一的原因就是开发机与 CI 都在 Asia/Shanghai：本地时区 == 账号时区，
     * 两套口径重合，缺陷完全隐形。测试环境跟生产账号同区，等于把这类 bug 的探测能力关掉。
     *
     * 挑 America/Los_Angeles 是因为它同时踩三个坑：与 Asia/Shanghai 差 15/16 小时
     * （足以跨日）、在西半球（偏移为负）、且有夏令时（DST 反算有专门的用例）。
     *
     * 用 `test.env` 而不是改 `package.json` 的脚本，是为了 `pnpm test` / `pnpm test:watch`
     * / 编辑器里点单个用例跑，走的都是同一个时区——从脚本传 TZ 只能盖住命令行那一条路。
     */
    env: { TZ: "America/Los_Angeles" },
    // e2e 归 playwright 管，vitest 只跑组件与纯函数
    include: ["**/*.test.ts", "**/*.test.tsx"],
    exclude: ["node_modules/**", "out/**", ".next/**", "e2e/**"],
  },
});
