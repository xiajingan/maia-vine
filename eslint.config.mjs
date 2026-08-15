// ⚠️ MAI-Harness 框架文件 — 请勿在项目中修改。如需变更请在框架工程中修改并覆盖到此项目。

/**
 * ESLint Configuration — Extends Harness Engineering rules
 *
 * Harness 规则来源：.harness/eslint/eslint.config.mjs
 *
 * 自定义方式：
 * 1. 在下方 overrides 中添加项目特定规则
 * 2. 仅使用基础规则（不含架构检查）：
 *    import { plugin, base } from './.harness/eslint/eslint.config.mjs';
 *    export default [plugin, base, { ignores: ['dist/'] }];
 */

import harness from './.harness/eslint/eslint.config.mjs';

export default [
  ...harness,

  // 全局忽略
  {
    ignores: ['dist/', 'build/', 'node_modules/', 'coverage/', '.harness/'],
  },

  // 项目特定规则覆盖（按需取消注释）
  // {
  //   files: ['src/**/*.test.ts'],
  //   rules: {
  //     'max-lines-per-function': 'off',
  //   },
  // },
];
