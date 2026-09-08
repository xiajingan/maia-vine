/**
 * eslint-plugin-harness — Harness Engineering 自定义 ESLint 规则
 *
 * H-01: 分层架构强制执行 (layer-imports)
 * H-02: 编码约束强制执行 (no-hardcoded-secrets, no-sql-concatenation)
 * H-03: 所有错误消息包含修复指令 + 规范引用
 * H-17: 黄金原则 Lint 化 (no-magic-values, no-duplicate-helper)
 *
 * 零依赖，纯 JS AST 分析，兼容任何 ESLint parser。
 */

// ─── 分层架构定义 ───────────────────────────────────────────────────────────────
// 默认 simple-layered Profile。项目可通过 rule options 提供 Architecture
// 声明的层名、文件/导入模式和依赖方向，不强制所有项目使用这组名称。
const DEFAULT_LAYERS = {
  controller: {
    display: 'Controller', filePattern: '\\.ctrl\\.[jt]sx?$', importPattern: '\\.ctrl\\b',
    allow: ['service', 'types', 'config'],
  },
  service: {
    display: 'Service', filePattern: '\\.svc\\.[jt]sx?$', importPattern: '\\.svc\\b',
    allow: ['repository', 'types', 'config'],
  },
  repository: {
    display: 'Repository', filePattern: '\\.repo\\.[jt]sx?$', importPattern: '\\.repo\\b',
    allow: ['types', 'config'],
  },
  types: {
    display: 'Types', filePattern: '(?:^|/)types\\.[jt]sx?$', importPattern: '(?:^|/)types(?:$|/)',
    allow: [],
  },
  config: {
    display: 'Config', filePattern: '(?:^|/)config\\.[jt]sx?$', importPattern: '(?:^|/)config(?:$|/)',
    allow: [],
  },
};

// 默认 simple-layered Profile 中 application/service 隔离的传输框架模块。
const HTTP_MODULES = [
  'fastify', 'express', 'koa', 'hapi', 'http', 'https',
  '@fastify/', '@hono/', 'hono', 'next/server',
];

// ─── 检测函数 ───────────────────────────────────────────────────────────────────

function layerRules(options = {}) {
  return options.layers || DEFAULT_LAYERS;
}

function compilePattern(pattern, label) {
  try {
    return new RegExp(pattern);
  } catch (error) {
    throw new TypeError(`harness/layer-imports 配置错误：${label} 不是合法正则 (${error.message})`);
  }
}

function compileLayerPolicy(options = {}) {
  const layers = layerRules(options);
  const names = new Set(Object.keys(layers));
  const compiledLayers = Object.fromEntries(Object.entries(layers).map(([name, rule]) => {
    for (const target of rule.allow) {
      if (!names.has(target)) {
        throw new TypeError(`harness/layer-imports 配置错误：${name}.allow 引用了未知层 ${target}`);
      }
    }
    return [name, {
      file: compilePattern(rule.filePattern, `${name}.filePattern`),
      imported: compilePattern(rule.importPattern, `${name}.importPattern`),
    }];
  }));
  const isolated = options.transportIsolatedLayers || (names.has('service') ? ['service'] : []);
  for (const name of isolated) {
    if (!names.has(name)) {
      throw new TypeError(`harness/layer-imports 配置错误：transportIsolatedLayers 引用了未知层 ${name}`);
    }
  }
  return {
    layers,
    compiledLayers,
    alwaysAllowed: (options.alwaysAllowedPatterns || ['(?:^|[/\\\\])(?:providers|shared)(?:[/\\\\]|$)'])
      .map((pattern, index) => compilePattern(pattern, `alwaysAllowedPatterns[${index}]`)),
    projectImports: (options.projectImportPatterns || ['^@/', '^~/', '^src/'])
      .map((pattern, index) => compilePattern(pattern, `projectImportPatterns[${index}]`)),
    transportIsolated: isolated,
    transportModules: options.transportModules || HTTP_MODULES,
  };
}

function detectLayer(value, policy, patternName) {
  for (const [name, patterns] of Object.entries(policy.compiledLayers)) {
    if (patterns[patternName].test(value)) return name;
  }
  return null;
}

function isProjectImport(source, policy) {
  return source.startsWith('.') || policy.projectImports.some(pattern => pattern.test(source));
}

// ─── 密钥检测模式 ───────────────────────────────────────────────────────────────

const SECRET_PATTERNS = [
  { pattern: /(?:api[_-]?key|apikey)\s*[:=]\s*['"][^'"]{8,}['"]/i, name: 'API Key' },
  { pattern: /(?:secret|token|password|passwd|pwd)\s*[:=]\s*['"][^'"]{8,}['"]/i, name: 'Secret/Token/Password' },
  { pattern: /AKIA[0-9A-Z]{16}/, name: 'AWS Access Key' },
  { pattern: /gh[ps]_[A-Za-z0-9_]{36,}/, name: 'GitHub Token' },
  { pattern: /(?:key|secret|token|password|credential)\s*[:=]\s*['"][A-Za-z0-9+/=]{32,}['"]/i, name: '硬编码凭据' },
  { pattern: /-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----/, name: '私钥' },
];

// ─── SQL 检测 ────────────────────────────────────────────────────────────────────

const SQL_KEYWORDS = /\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|EXECUTE|UNION|TRUNCATE)\b/i;

// ═══════════════════════════════════════════════════════════════════════════════
// Plugin 定义
// ═══════════════════════════════════════════════════════════════════════════════

export default {
  meta: { name: 'eslint-plugin-harness', version: '1.2.0' },
  rules: {

    // ─── H-01: 分层架构强制执行 ─────────────────────────────────────────────────
    'layer-imports': {
      meta: {
        type: 'problem',
        docs: {
          description: '根据项目 Architecture Profile 强制执行分层依赖方向',
        },
        messages: {
          layerViolation:
            '{{ currentLayer }} 层不允许导入 {{ importLayer }} 层。' +
            '允许的依赖：{{ allowed }}。' +
            '请将此逻辑移至正确边界，或同步修正 ARCHITECTURE.md 与 ESLint architecture policy。',
          httpInService:
            '当前层按 Architecture Profile 与传输框架隔离，不允许导入「{{ source }}」。' +
            '请把传输适配移至项目声明的 adapter/delivery 边界。',
        },
        schema: [{
          type: 'object',
          properties: {
            layers: {
              type: 'object',
              minProperties: 1,
              additionalProperties: {
                type: 'object',
                required: ['filePattern', 'importPattern', 'allow'],
                properties: {
                  display: { type: 'string' },
                  filePattern: { type: 'string' },
                  importPattern: { type: 'string' },
                  allow: { type: 'array', items: { type: 'string' }, uniqueItems: true },
                },
                additionalProperties: false,
              },
            },
            alwaysAllowedPatterns: { type: 'array', items: { type: 'string' }, uniqueItems: true },
            projectImportPatterns: { type: 'array', items: { type: 'string' }, uniqueItems: true },
            transportIsolatedLayers: { type: 'array', items: { type: 'string' }, uniqueItems: true },
            transportModules: { type: 'array', items: { type: 'string' }, uniqueItems: true },
          },
          additionalProperties: false,
        }],
      },
      create(context) {
        const options = context.options[0] || {};
        const policy = compileLayerPolicy(options);
        const layers = policy.layers;
        const filename = context.filename || context.getFilename();
        const currentLayer = detectLayer(filename, policy, 'file');
        if (!currentLayer) return {};

        const allowed = layers[currentLayer]?.allow || [];

        return {
          ImportDeclaration(node) {
            const source = node.source.value;
            if (policy.alwaysAllowed.some(pattern => pattern.test(source))) return;

            if (
              policy.transportIsolated.includes(currentLayer)
              && policy.transportModules.some(mod => source === mod || source.startsWith(mod))
            ) {
              context.report({ node, messageId: 'httpInService', data: { source } });
              return;
            }

            // 相对路径与项目显式 alias 使用同一依赖方向检查；外部包不猜测层级。
            if (isProjectImport(source, policy)) {
              const importLayer = detectLayer(source, policy, 'imported');
              if (importLayer && importLayer !== currentLayer && !allowed.includes(importLayer)) {
                context.report({
                  node,
                  messageId: 'layerViolation',
                  data: {
                    currentLayer: layers[currentLayer]?.display || currentLayer,
                    importLayer: layers[importLayer]?.display || importLayer,
                    allowed: [currentLayer, ...allowed].map(l => layers[l]?.display || l).join('、'),
                  },
                });
              }
            }
          },
        };
      },
    },

    // ─── H-02: 硬编码密钥检测 ───────────────────────────────────────────────────
    'no-hardcoded-secrets': {
      meta: {
        type: 'problem',
        docs: { description: '禁止在源码中硬编码密钥、API Key、Token、密码' },
        messages: {
          secretDetected:
            '检测到疑似硬编码的{{ type }}。禁止将密钥提交到代码仓库。' +
            '请通过环境变量注入（process.env.XXX），并在 .env.example 中添加占位符（无真实值）。' +
            '参考 docs/CODING_BACKEND.md §密钥管理 / PROJECT_RULES.md §Key Rules #4。',
        },
        schema: [],
      },
      create(context) {
        function check(node, value) {
          if (typeof value !== 'string') return;
          for (const { pattern, name } of SECRET_PATTERNS) {
            if (pattern.test(value)) {
              context.report({ node, messageId: 'secretDetected', data: { type: name } });
              return;
            }
          }
        }
        return {
          Literal(node) { check(node, node.value); },
          TemplateLiteral(node) {
            for (const quasi of node.quasis) { check(node, quasi.value.raw); }
          },
        };
      },
    },

    // ─── H-02: SQL 拼接检测 ─────────────────────────────────────────────────────
    'no-sql-concatenation': {
      meta: {
        type: 'problem',
        docs: { description: '禁止字符串拼接 SQL，必须使用 ORM 参数化查询' },
        messages: {
          sqlConcat:
            '检测到 SQL 字符串拼接，存在 SQL 注入风险。' +
            '数据库查询必须使用 ORM 参数化查询（如 Drizzle/Prisma），禁止字符串拼接 SQL。' +
            '参考 docs/CODING_BACKEND.md §数据安全 / PROJECT_RULES.md §Key Rules #6。',
        },
        schema: [],
      },
      create(context) {
        return {
          TemplateLiteral(node) {
            if (node.expressions.length === 0) return;
            const raw = node.quasis.map(q => q.value.raw).join('__EXPR__');
            if (SQL_KEYWORDS.test(raw)) {
              context.report({ node, messageId: 'sqlConcat' });
            }
          },
          BinaryExpression(node) {
            if (node.operator !== '+') return;
            function hasSQL(n) {
              if (n.type === 'Literal' && typeof n.value === 'string') return SQL_KEYWORDS.test(n.value);
              if (n.type === 'TemplateLiteral') return n.quasis.some(q => SQL_KEYWORDS.test(q.value.raw));
              if (n.type === 'BinaryExpression' && n.operator === '+') return hasSQL(n.left) || hasSQL(n.right);
              return false;
            }
            function hasVar(n) {
              if (n.type === 'Identifier' || n.type === 'MemberExpression' || n.type === 'CallExpression') return true;
              if (n.type === 'BinaryExpression') return hasVar(n.left) || hasVar(n.right);
              return false;
            }
            if ((hasSQL(node.left) || hasSQL(node.right)) && (hasVar(node.left) || hasVar(node.right))) {
              context.report({ node, messageId: 'sqlConcat' });
            }
          },
        };
      },
    },

    // ─── H-17: 魔法数字检测（黄金原则 G-3） ─────────────────────────────────────
    'no-magic-values': {
      meta: {
        type: 'suggestion',
        docs: {
          description: '禁止硬编码魔法数字，业务常量应集中到 config/ 或 shared/constants',
        },
        messages: {
          magicNumber:
            '硬编码数字 {{ value }} 应提取为命名常量。' +
            '业务阈值、配置值应集中到 config/ 或 shared/constants。' +
            '参考 docs/GOLDEN_RULES.md §G-3。',
        },
        schema: [{
          type: 'object',
          properties: {
            ignore: { type: 'array', items: { type: 'number' } },
          },
          additionalProperties: false,
        }],
      },
      create(context) {
        const DEFAULT_IGNORE = [0, 1, -1, 2, 10, 100, 1000];
        const options = context.options[0] || {};
        const ignored = new Set(options.ignore || DEFAULT_IGNORE);

        function isConstDeclaration(node) {
          let parent = node.parent;
          // const X = 42
          while (parent) {
            if (parent.type === 'VariableDeclaration' && parent.kind === 'const') return true;
            if (parent.type === 'Property' || parent.type === 'ArrayExpression') {
              parent = parent.parent;
              continue;
            }
            break;
          }
          return false;
        }

        function isDefaultParam(node) {
          return node.parent && node.parent.type === 'AssignmentPattern';
        }

        function isEnumOrType(node) {
          let p = node.parent;
          while (p) {
            if (p.type === 'TSEnumDeclaration' || p.type === 'TSTypeAliasDeclaration') return true;
            p = p.parent;
          }
          return false;
        }

        return {
          Literal(node) {
            if (typeof node.value !== 'number') return;
            if (ignored.has(node.value)) return;
            if (!Number.isFinite(node.value)) return;
            if (isConstDeclaration(node)) return;
            if (isDefaultParam(node)) return;
            if (isEnumOrType(node)) return;

            // Skip array index patterns: arr[N]
            if (node.parent && node.parent.type === 'MemberExpression' && node.parent.computed && node.parent.property === node) return;

            context.report({
              node,
              messageId: 'magicNumber',
              data: { value: String(node.value) },
            });
          },
        };
      },
    },

    // ─── H-17: 散落工具函数检测（黄金原则 G-1） ─────────────────────────────────
    'no-duplicate-helper': {
      meta: {
        type: 'suggestion',
        docs: {
          description: '工具函数应集中在 shared/ 目录，禁止在模块内散落 util/helper 文件',
        },
        messages: {
          helperOutsideShared:
            '文件名「{{ filename }}」匹配工具函数模式（util/helper/common），但不在 shared/ 目录下。' +
            '请将公共工具函数提取到 shared/ 目录，避免跨模块重复。' +
            '参考 docs/GOLDEN_RULES.md §G-1。',
        },
        schema: [{
          type: 'object',
          properties: {
            sharedDirs: { type: 'array', items: { type: 'string' } },
          },
          additionalProperties: false,
        }],
      },
      create(context) {
        const options = context.options[0] || {};
        const sharedDirs = options.sharedDirs || ['shared', 'common', 'lib'];
        const HELPER_NAME = /(?:^|[/\\])(?:utils?|helpers?|common)\.[mc]?[jt]sx?$/i;

        const filename = context.filename || context.getFilename();

        // Only trigger for files matching helper patterns
        if (!HELPER_NAME.test(filename)) return {};

        // Check if file is inside a shared directory
        const inShared = sharedDirs.some(dir => {
          const sep = /[/\\]/;
          return filename.split(sep).includes(dir);
        });

        if (inShared) return {};

        return {
          Program(node) {
            const basename = filename.split(/[/\\]/).pop();
            context.report({
              node,
              messageId: 'helperOutsideShared',
              data: { filename: basename },
            });
          },
        };
      },
    },
    // ─── CICD-01: 禁止直接调用 VCS CLI（CICD.md）─────────────────────────
    // 必须通过 src/mai_harness/runtime/commands/pr_adapter.py 统一接口创建/合并 PR/MR。
    'no-direct-vcs-cli': {
      meta: {
        type: 'problem',
        docs: { description: '禁止源码中直接调用 gh pr / glab mr，统一走 pr_adapter.py' },
        messages: {
          directCli:
            '检测到直接调用 {{ tool }} 操作 PR/MR：「{{ snippet }}」。' +
            '请改为 `uv run --project .harness/runtime harness pr-adapter create|status|merge|comment`，以兼容 GitHub 与 GitLab。' +
            '参考 docs/CICD.md。',
        },
        schema: [],
      },
      create(context) {
        // 允许 pr_adapter.py 自身使用 gh/glab
        const filename = context.filename || context.getFilename();
        if (/pr_adapter\.py$/.test(filename)) return {};

        const PATTERNS = [
          { tool: 'gh',   re: /\bgh\s+pr\s+(create|merge|comment|view|checks|review|edit|close|reopen|ready)\b/ },
          { tool: 'glab', re: /\bglab\s+mr\s+(create|merge|note|view|update|approve|close|reopen)\b/ },
        ];

        function check(node, value) {
          if (typeof value !== 'string') return;
          for (const { tool, re } of PATTERNS) {
            const m = value.match(re);
            if (m) {
              context.report({
                node,
                messageId: 'directCli',
                data: { tool, snippet: m[0].slice(0, 80) },
              });
              return;
            }
          }
        }

        return {
          Literal(node) { check(node, node.value); },
          TemplateLiteral(node) {
            for (const q of node.quasis) check(node, q.value.raw);
          },
          // 数组形式：spawn(['gh', 'pr', 'create', ...]) / execFile('glab', ['mr', ...])
          ArrayExpression(node) {
            const head = node.elements?.[0];
            const second = node.elements?.[1];
            if (!head || head.type !== 'Literal') return;
            const tool = head.value;
            if (tool !== 'gh' && tool !== 'glab') return;
            if (!second || second.type !== 'Literal') return;
            const sub = second.value;
            if (tool === 'gh' && sub === 'pr') {
              context.report({ node, messageId: 'directCli', data: { tool, snippet: `[${tool}, ${sub}, ...]` } });
            } else if (tool === 'glab' && sub === 'mr') {
              context.report({ node, messageId: 'directCli', data: { tool, snippet: `[${tool}, ${sub}, ...]` } });
            }
          },
          // execFile / spawn 第一参数为 'gh'/'glab'，第二参数数组首元素为 pr/mr
          CallExpression(node) {
            const callee = node.callee;
            const name = callee?.name || callee?.property?.name;
            if (!/^(spawn|spawnSync|execFile|execFileSync)$/.test(name || '')) return;
            const a0 = node.arguments?.[0];
            const a1 = node.arguments?.[1];
            if (!a0 || a0.type !== 'Literal') return;
            const tool = a0.value;
            if (tool !== 'gh' && tool !== 'glab') return;
            if (!a1 || a1.type !== 'ArrayExpression') return;
            const sub = a1.elements?.[0];
            if (!sub || sub.type !== 'Literal') return;
            if ((tool === 'gh' && sub.value === 'pr') || (tool === 'glab' && sub.value === 'mr')) {
              context.report({ node, messageId: 'directCli', data: { tool, snippet: `${name}(${tool}, [${sub.value}, ...])` } });
            }
          },
        };
      },
    },
  },
};
