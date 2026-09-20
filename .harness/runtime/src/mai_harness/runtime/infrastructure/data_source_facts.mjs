// Syntax extraction only. Golden Rule decisions live in the Python data-lint engine.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const request = JSON.parse(readFileSync(process.argv[2], 'utf8'));
function dependency(name) {
  for (const path of [resolve(request.root, 'package.json'), ...request.files]) {
    try { return createRequire(path)(name); } catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }
  }
  throw new Error(`data-lint 需要工程依赖 ${name}；安装项目锁定依赖后重试`);
}
const ts = dependency('typescript');
if (!ts.createProgram) throw new Error('data-lint 需要提供 Compiler API 的 TypeScript 5/6');
const virtual = new Map();
for (const file of request.files) {
  if (!file.endsWith('.vue')) continue;
  const source = readFileSync(file, 'utf8');
  const { descriptor, errors } = dependency('@vue/compiler-sfc').parse(source);
  if (errors.length) throw new Error(`${file}: Vue SFC 解析失败`);
  const content = source.split('').map((c) => c === '\n' ? '\n' : ' ');
  for (const block of [descriptor.script, descriptor.scriptSetup].filter(Boolean)) {
    for (let i = 0; i < block.content.length; i++) content[block.loc.start.offset + i] = block.content[i];
  }
  virtual.set(`${file}.ts`, content.join(''));
}
const names = request.files.map((file) => file.endsWith('.vue') ? `${file}.ts` : file);
const options = { allowJs: true, noResolve: true, noLib: true, noEmit: true, target: ts.ScriptTarget.Latest };
const host = ts.createCompilerHost(options);
const originalRead = host.readFile.bind(host);
host.readFile = (file) => virtual.get(file) ?? originalRead(file);
const program = ts.createProgram(names, options, host);
const checker = program.getTypeChecker();
const facts = [];
const mutatedOptions = new Set();

function unwrap(node) {
  while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
    || ts.isTypeAssertionExpression(node) || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node))) {
    node = node.expression;
  }
  return node;
}
function value(node, seen = new Set()) {
  node = unwrap(node);
  if (!node || seen.has(node)) return node;
  seen.add(node);
  if (ts.isIdentifier(node)) {
    const declaration = checker.getSymbolAtLocation(node)?.valueDeclaration;
    if (declaration && ts.isVariableDeclaration(declaration) && declaration.initializer
      && (declaration.parent.flags & ts.NodeFlags.Const)) return value(declaration.initializer, seen);
  }
  return node;
}
function name(node) {
  if (!node) return '';
  if (ts.isComputedPropertyName(node)) return name(value(node.expression));
  if (ts.isElementAccessExpression(node)) return name(value(node.argumentExpression));
  if (ts.isPropertyAccessExpression(node)) return name(node.name);
  if (ts.isIdentifier(node)) {
    const declaration = checker.getSymbolAtLocation(node)?.declarations?.[0];
    if (declaration && ts.isImportSpecifier(declaration)) return (declaration.propertyName ?? declaration.name).text;
  }
  return node.text ?? '';
}
function chain(node) {
  node = value(node);
  if (!node) return [];
  if (ts.isIdentifier(node)) {
    const declaration = checker.getSymbolAtLocation(node)?.valueDeclaration;
    if (declaration && ts.isBindingElement(declaration)) {
      const variable = declaration.parent.parent;
      if (ts.isVariableDeclaration(variable) && variable.initializer) {
        return [...chain(variable.initializer), name(declaration.propertyName ?? declaration.name)];
      }
    }
  }
  if (ts.isCallExpression(node)) return chain(node.expression);
  if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
    return [...chain(node.expression), name(node)];
  }
  return [name(node)];
}
function properties(node, seen = new Set()) {
  node = value(node);
  if (mutatedOptions.has(node)) return { keys: [], dynamic: true };
  if (!node || seen.has(node) || !ts.isObjectLiteralExpression(node)) return { keys: [], dynamic: true };
  seen.add(node);
  const result = { keys: [], dynamic: false };
  for (const entry of node.properties) {
    if (ts.isSpreadAssignment(entry)) {
      const spread = properties(entry.expression, new Set(seen));
      result.keys.push(...spread.keys); result.dynamic ||= spread.dynamic;
    } else {
      const key = name(entry.name);
      if (key) result.keys.push(key); else result.dynamic = true;
    }
  }
  return result;
}
function constant(node) {
  node = value(node);
  if (!node) return null;
  if (ts.isStringLiteralLike(node)) return node.text;
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = constant(node.left), right = constant(node.right);
    return left !== null && right !== null ? left + right : null;
  }
  return null;
}
function template(node) {
  if (ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  return node.head.text + node.templateSpans.map((span) => ` __parameter__ ${span.literal.text}`).join('');
}
function numericType(type) {
  return type && (type.kind === ts.SyntaxKind.NumberKeyword
    || (ts.isUnionTypeNode(type) && type.types.some(numericType)));
}
function nestedCalls(node) {
  const calls = [];
  function visit(child) {
    if (ts.isCallExpression(child)) calls.push(chain(child.expression).at(-1));
    ts.forEachChild(child, visit);
  }
  visit(node);
  return calls;
}

// An initializer is not a complete options object when later writes can add `with`.
for (const file of names) {
  function collect(node) {
    if (ts.isBinaryExpression(node) && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
      && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment) {
      const target = node.left;
      if ((ts.isPropertyAccessExpression(target) || ts.isElementAccessExpression(target))
        && (!name(target) || name(target) === 'with')) mutatedOptions.add(value(target.expression));
    }
    if (ts.isCallExpression(node)) {
      const path = chain(node.expression);
      if (path[0] === 'Object' && ['assign', 'defineProperty', 'defineProperties', 'setPrototypeOf'].includes(path.at(-1))) {
        mutatedOptions.add(value(node.arguments[0]));
      }
    }
    ts.forEachChild(node, collect);
  }
  const source = program.getSourceFile(file);
  if (source) collect(source);
}

for (const file of names) {
  const source = program.getSourceFile(file);
  if (!source) throw new Error(`${file}: 无法解析源文件`);
  const realFile = virtual.has(file) ? file.slice(0, -3) : file;
  function emit(node, kind, detail = {}) {
    facts.push({ file: realFile, line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1, kind, ...detail });
  }
  for (const diagnostic of source.parseDiagnostics) {
    facts.push({ file: realFile, line: source.getLineAndCharacterOfPosition(diagnostic.start ?? 0).line + 1,
      kind: 'parse_error', message: ts.flattenDiagnosticMessageText(diagnostic.messageText, ' ') });
  }
  function sqlArgument(node, argument, raw = false) {
    const text = constant(argument);
    if (text !== null) emit(node, 'sql', { text });
    else {
      const target = value(argument);
      if (raw || (target && (ts.isTemplateExpression(target) || ts.isBinaryExpression(target)))) {
        emit(node, 'dynamic_sql');
      }
    }
  }
  function visit(node) {
    if (ts.isTaggedTemplateExpression(node) && chain(node.tag).at(-1) === 'sql') {
      emit(node, 'sql', { text: template(node.template) });
    }
    if (ts.isCallExpression(node)) {
      const path = chain(node.expression), method = path.at(-1);
      if (['execute', 'query', 'text', 'sql'].includes(method) && node.arguments.length) {
        sqlArgument(node, node.arguments[0]);
      }
      if (method === 'raw' && path.includes('sql') && node.arguments.length) sqlArgument(node, node.arguments[0], true);
      if (['findMany', 'findFirst'].includes(method) && path.includes('query') && node.arguments.length) {
        const fields = properties(node.arguments[0]);
        if (fields.keys.includes('with') || fields.dynamic) emit(node, 'relation', { dynamic: fields.dynamic });
      }
      if (['Number', 'parseInt', 'parseFloat'].includes(method) && node.arguments.length) {
        const argument = value(node.arguments[0]);
        emit(node, 'numeric_id', { name: name(node.arguments[0]), resolvedName: name(argument), operation: method });
      }
    }
    if (ts.isPrefixUnaryExpression(node) && node.operator === ts.SyntaxKind.PlusToken) {
      emit(node, 'numeric_id', { name: name(node.operand), resolvedName: name(value(node.operand)), operation: '+' });
    }
    if ((ts.isAsExpression(node) || ts.isTypeAssertionExpression(node)) && numericType(node.type)) {
      emit(node, 'numeric_id', { name: name(node.expression), resolvedName: name(value(node.expression)), operation: 'as number' });
    }
    if ((ts.isPropertySignature(node) || ts.isPropertyDeclaration(node) || ts.isParameter(node)
      || ts.isVariableDeclaration(node)) && numericType(node.type)) {
      emit(node, 'numeric_id', { name: name(node.name), operation: 'number 类型' });
    }
    if (ts.isPropertyAssignment(node)) {
      const field = name(node.name), initializer = value(node.initializer);
      if (initializer && ts.isCallExpression(initializer)) {
        let current = initializer;
        const generators = [], calls = [];
        while (current && ts.isCallExpression(current)) {
          const path = chain(current.expression), method = path.at(-1);
          calls.push({ method, args: current.arguments });
          generators.push(method);
          for (const arg of current.arguments) {
            generators.push(...nestedCalls(arg));
            if (ts.isIdentifier(arg)) generators.push(name(arg));
          }
          current = (ts.isPropertyAccessExpression(current.expression) || ts.isElementAccessExpression(current.expression))
            ? value(current.expression.expression) : null;
        }
        const builder = calls.at(-1);
        const numeric = ['Number', 'Integer', 'number', 'int', 'integer', 'float', 'double', 'serial', 'smallint', 'tinyint', 'mediumint'];
        if (builder) {
          const columnName = constant(builder.args[0]) ?? field;
          const mode = builder.args.map((arg) => value(arg)).filter((arg) => arg && ts.isObjectLiteralExpression(arg))
            .flatMap((arg) => arg.properties).find((entry) => name(entry.name) === 'mode');
          emit(node, 'id_builder', { name: field, columnName, builder: builder.method,
            mode: mode ? constant(mode.initializer) : null,
            numeric: Boolean(numeric.includes(builder.method) || (mode && constant(mode.initializer) === 'number')), generators });
        }
      }
      if (initializer && ts.isObjectLiteralExpression(initializer)) {
        const type = initializer.properties.find((entry) => name(entry.name) === 'type');
        if (type && ['number', 'integer'].includes(constant(type.initializer))) {
          emit(node, 'numeric_id', { name: field, operation: 'JSON Schema 数值类型' });
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}
process.stdout.write(JSON.stringify(facts));
