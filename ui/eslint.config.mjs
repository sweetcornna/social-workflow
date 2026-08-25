import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({ baseDirectory: __dirname });

const config = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    ignores: [
      "out/**",
      ".next/**",
      "node_modules/**",
      "playwright-report/**",
      "test-results/**",
      // Next 生成的声明文件，内容不归我们管
      "next-env.d.ts",
    ],
  },
  {
    // tailwind 插件只有 CJS 入口，这里必须 require
    files: ["tailwind.config.ts"],
    rules: { "@typescript-eslint/no-require-imports": "off" },
  },
  {
    rules: {
      // 中文注释英文标识符是本项目的约定，这条规则会误伤中文字符串
      "react/no-unescaped-entities": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
];

export default config;
