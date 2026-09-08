/**
 * Harness Engineering — ESLint Flat Config（可组合）
 *
 * 将 CODING_BACKEND.md / CODING_FRONTEND.md / PROJECT_RULES.md 中的
 * 硬性约束编码为可机械执行的 ESLint 规则。
 *
 * 使用方式（项目 eslint.config.mjs）：
 *   import harness from './lint/eslint.config.mjs';
 *   export default [...harness, { ignores: ['dist/'] }];
 *
 * 按需组合：
 *   import { plugin, base, architecture } from './lint/eslint.config.mjs';
 *   export default [plugin, base]; // 仅基础规则，跳过架构检查
 */

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import harnessPlugin from './harness-plugin.mjs';

// ─── 插件注册（必须包含，放在最前） ──────────────────────────────────────────────

export const plugin = {
  plugins: { harness: harnessPlugin },
};

// ─── H-02: 通用编码约束 ─────────────────────────────────────────────────────────
// 适用于所有 JS/TS 项目，无需特定 parser

export const base = {
  rules: {
    // PROJECT_RULES.md §Key Rules #1: 禁止空 catch 块，至少记录日志
    'no-empty': ['error', { allowEmptyCatch: false }],

    // PROJECT_RULES.md §Key Rules #2: 函数体 ≤ 50 行
    'max-lines-per-function': ['error', {
      max: 50, skipBlankLines: true, skipComments: true,
    }],

    // PROJECT_RULES.md §Key Rules #3: 圈复杂度 ≤ 10
    'complexity': ['error', { max: 10 }],

    // PROJECT_RULES.md §Key Rules #7: 文件 ≤ 300 行
    'max-lines': ['error', { max: 300, skipBlankLines: true, skipComments: true }],

    // PROJECT_RULES.md §Key Rules #5: 结构化日志，禁止 console.log
    'no-console': 'error',

    // PROJECT_RULES.md §禁止事项: 禁止 eval / Function() / 动态执行
    'no-eval': 'error',
    'no-implied-eval': 'error',
    'no-new-func': 'error',

    // PROJECT_RULES.md §禁止事项: 禁止循环中数据库/API 调用（无法静态检测，但可检测 await-in-loop）
    'no-await-in-loop': 'warn',

    // PROJECT_RULES.md §Key Rules #4: 禁止硬编码密钥
    'harness/no-hardcoded-secrets': 'error',

    // PROJECT_RULES.md §Key Rules #6: 禁止 SQL 拼接
    'harness/no-sql-concatenation': 'error',

    // GOLDEN_RULES.md §G-3: 魔法数字集中管理
    'harness/no-magic-values': ['warn', { ignore: [0, 1, -1, 2, 10, 100, 1000] }],

    // GOLDEN_RULES.md §G-1: 工具函数集中到 shared/
    'harness/no-duplicate-helper': 'warn',

    // CICD.md: 禁止源码直接调用 gh pr / glab mr，统一走 pr-adapter.mjs
    'harness/no-direct-vcs-cli': 'error',
  },
};

// ─── H-01: 分层架构强制执行 ──────────────────────────────────────────────────────
// 适用于模块化后端代码（src/modules/**）

export function architectureFor(policy = {}) {
  return {
    files: ['**/modules/**', '**/src/**'],
    rules: {
      // 空 policy 使用 Harness 的 simple-layered 默认 Profile；项目采用
      // domain-centric/event-driven/custom 时，传入 ARCHITECTURE.md 对应 policy。
      'harness/layer-imports': ['error', policy],
    },
  };
}

export const architecture = architectureFor();

const domainCentricLayers = {
  adapter: { filePattern: '(?:^|/)(?:adapters|controllers|api|routes)/', importPattern: '(?:^|/)(?:adapters|controllers|api|routes)(?:/|$)', allow: ['application', 'domain', 'ports', 'types', 'config', 'shared'] },
  application: { filePattern: '(?:^|/)(?:application|use-cases|use_cases)/', importPattern: '(?:^|/)(?:application|use-cases|use_cases)(?:/|$)', allow: ['domain', 'ports', 'types', 'config', 'shared'] },
  domain: { filePattern: '(?:^|/)domain/', importPattern: '(?:^|/)domain(?:/|$)', allow: ['types', 'shared'] },
  ports: { filePattern: '(?:^|/)ports/', importPattern: '(?:^|/)ports(?:/|$)', allow: ['domain', 'types', 'shared'] },
  infrastructure: { filePattern: '(?:^|/)(?:infrastructure|persistence|clients|repositories)/', importPattern: '(?:^|/)(?:infrastructure|persistence|clients|repositories)(?:/|$)', allow: ['domain', 'ports', 'types', 'config', 'shared'] },
  types: { filePattern: '(?:^|/)types(?:/|\\.)', importPattern: '(?:^|/)types(?:/|$)', allow: [] },
  config: { filePattern: '(?:^|/)config(?:/|\\.)', importPattern: '(?:^|/)config(?:/|$)', allow: [] },
  shared: { filePattern: '(?:^|/)(?:shared|common|lib)/', importPattern: '(?:^|/)(?:shared|common|lib)(?:/|$)', allow: ['types', 'config'] },
};

const eventDrivenLayers = {
  transport: { filePattern: '(?:^|/)(?:producers|consumers|handlers|api)/', importPattern: '(?:^|/)(?:producers|consumers|handlers|api)(?:/|$)', allow: ['application', 'domain', 'contracts', 'ports', 'types', 'config', 'shared'] },
  application: { filePattern: '(?:^|/)(?:application|use-cases|use_cases)/', importPattern: '(?:^|/)(?:application|use-cases|use_cases)(?:/|$)', allow: ['domain', 'contracts', 'ports', 'types', 'config', 'shared'] },
  domain: { filePattern: '(?:^|/)domain/', importPattern: '(?:^|/)domain(?:/|$)', allow: ['contracts', 'types', 'shared'] },
  ports: { filePattern: '(?:^|/)ports/', importPattern: '(?:^|/)ports(?:/|$)', allow: ['domain', 'contracts', 'types', 'shared'] },
  infrastructure: { filePattern: '(?:^|/)(?:infrastructure|persistence|clients)/', importPattern: '(?:^|/)(?:infrastructure|persistence|clients)(?:/|$)', allow: ['domain', 'contracts', 'ports', 'types', 'config', 'shared'] },
  contracts: { filePattern: '(?:^|/)(?:contracts|events)/', importPattern: '(?:^|/)(?:contracts|events)(?:/|$)', allow: ['types', 'shared'] },
  types: { filePattern: '(?:^|/)types(?:/|\\.)', importPattern: '(?:^|/)types(?:/|$)', allow: [] },
  config: { filePattern: '(?:^|/)config(?:/|\\.)', importPattern: '(?:^|/)config(?:/|$)', allow: [] },
  shared: { filePattern: '(?:^|/)(?:shared|common|lib)/', importPattern: '(?:^|/)(?:shared|common|lib)(?:/|$)', allow: ['types', 'config'] },
};

export function architectureProfileFromHarnessConfig(content) {
  const section = content.match(/^architecture:\s*\n((?:^[ \t]+.*(?:\n|$))*)/m)?.[1] || '';
  const block = section.match(/^\s+profile:\s*['"]?([a-z-]+)['"]?\s*(?:#.*)?$/m)?.[1];
  const inline = content.match(/^architecture:\s*\{[^}\n]*\bprofile:\s*['"]?([a-z-]+)['"]?[^}\n]*\}\s*$/m)?.[1];
  const profile = block || inline;
  if (!profile || !['simple-layered', 'domain-centric', 'event-driven', 'custom'].includes(profile)) {
    throw new TypeError('config/harness.yml 缺少合法 architecture.profile');
  }
  return profile;
}

export function architectureProfileFromDocument(content) {
  const profile = content.match(/^\*\*当前 Profile\*\*\s*[:：]\s*`?([a-z-]+)`?\s*$/m)?.[1];
  if (!profile || !['simple-layered', 'domain-centric', 'event-driven', 'custom'].includes(profile)) {
    throw new TypeError('ARCHITECTURE.md 缺少合法的当前 Profile 声明');
  }
  return profile;
}

export function architectureForProfile(profile, customPolicy = {}) {
  if (profile === 'simple-layered') return architectureFor();
  if (profile === 'domain-centric') return architectureFor({ layers: domainCentricLayers, transportIsolatedLayers: ['application'] });
  if (profile === 'event-driven') return architectureFor({ layers: eventDrivenLayers, transportIsolatedLayers: ['application'] });
  if (profile === 'custom' && customPolicy.layers) return architectureFor(customPolicy);
  throw new TypeError(`Architecture Profile ${profile} 缺少可执行 ESLint policy`);
}

// ─── 测试文件放宽规则 ────────────────────────────────────────────────────────────
// 测试文件允许更长的函数和文件

export const testOverrides = {
  files: ['**/*.test.*', '**/*.spec.*', '**/__tests__/**', '**/test/**'],
  rules: {
    'max-lines-per-function': 'off',
    'max-lines': 'off',
  },
};

// ─── 配置文件豁免 ────────────────────────────────────────────────────────────────

export const configOverrides = {
  files: [
    '**/eslint.config.*', '**/vite.config.*', '**/vitest.config.*',
    '**/tailwind.config.*', '**/postcss.config.*', '**/drizzle.config.*',
    '**/next.config.*', '**/nuxt.config.*',
  ],
  rules: {
    'max-lines-per-function': 'off',
    'max-lines': 'off',
    'no-console': 'off',
  },
};

// ─── 默认导出：全部配置组合 ──────────────────────────────────────────────────────

export function harnessForProfile(profile, customPolicy = {}) {
  return [plugin, base, architectureForProfile(profile, customPolicy), testOverrides, configOverrides];
}

export function harnessFromProject(projectRoot = process.cwd()) {
  const profile = architectureProfileFromHarnessConfig(
    readFileSync(resolve(projectRoot, 'config/harness.yml'), 'utf8'),
  );
  const documented = architectureProfileFromDocument(
    readFileSync(resolve(projectRoot, 'ARCHITECTURE.md'), 'utf8'),
  );
  if (documented !== profile) {
    throw new TypeError(`ARCHITECTURE.md 当前 Profile ${documented} 与 config/harness.yml ${profile} 不一致`);
  }
  const customPath = resolve(projectRoot, 'config/architecture-eslint.json');
  const customPolicy = profile === 'custom' && existsSync(customPath)
    ? JSON.parse(readFileSync(customPath, 'utf8'))
    : {};
  return harnessForProfile(profile, customPolicy);
}

// 旧项目的 create-only eslint.config.mjs 通常导入默认 export；默认值也必须动态
// 消费项目配置，避免同步 Runtime 后继续静默执行 simple-layered。
export default harnessFromProject();
