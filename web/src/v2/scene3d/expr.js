// claygl's compositor sizes, without `new Function` (canvas-v2-phase3-4.md D8, 3.11).
//
// echarts-gl's post-effect graph sizes its buffers with strings such as
// "expr([width * dpr / 4, height * dpr / 4])", which claygl's createCompositor turns into a
// function with `new Function`. The page's CSP (script-src 'self') refuses that, so the build
// (vite.config.js, cspSafeClayglExpr) replaces the call with synapseExpr(source): a small
// recursive-descent evaluator of exactly that grammar, which runs nothing but arithmetic.
//
//   expr    := term (("+" | "-") term)*
//   term    := unary (("*" | "/") unary)*
//   unary   := ("+" | "-") unary | primary
//   primary := number | "width" | "height" | "dpr" | "(" expr ")" | "[" expr ("," expr)* "]"
//
// Anything else throws while parsing (claygl turns that into "Invalid expression."). The result is
// a plain function (width, height, dpr) -> number | number[].

const NAMES = new Set(["width", "height", "dpr"]);
const MAX_SOURCE = 256;
const MAX_DEPTH = 32;

function tokenize(source) {
  const out = [];
  let i = 0;
  while (i < source.length) {
    const c = source[i];
    if (c === " " || c === "\t" || c === "\n" || c === "\r") {
      i += 1;
    } else if ("+-*/()[],".includes(c)) {
      out.push({ t: c });
      i += 1;
    } else if ((c >= "0" && c <= "9") || c === ".") {
      const m = /^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/.exec(source.slice(i));
      if (!m) throw new SyntaxError(`bad number at ${i}`);
      out.push({ t: "num", v: Number(m[0]) });
      i += m[0].length;
    } else if (/[A-Za-z_]/.test(c)) {
      const m = /^[A-Za-z_][A-Za-z0-9_]*/.exec(source.slice(i));
      if (!NAMES.has(m[0])) throw new SyntaxError(`unknown name ${m[0]}`);
      out.push({ t: "name", v: m[0] });
      i += m[0].length;
    } else {
      throw new SyntaxError(`unexpected ${JSON.stringify(c)} at ${i}`);
    }
  }
  return out;
}

function parse(tokens) {
  let at = 0;
  const peek = () => tokens[at];
  const take = (t) => {
    const tok = tokens[at];
    if (!tok || tok.t !== t) throw new SyntaxError(`expected ${t}`);
    at += 1;
    return tok;
  };
  function expr(depth) {
    if (depth > MAX_DEPTH) throw new SyntaxError("too deep");
    let node = term(depth);
    while (peek() && (peek().t === "+" || peek().t === "-")) {
      const op = tokens[at++].t;
      node = { op, a: node, b: term(depth) };
    }
    return node;
  }
  function term(depth) {
    let node = unary(depth);
    while (peek() && (peek().t === "*" || peek().t === "/")) {
      const op = tokens[at++].t;
      node = { op, a: node, b: unary(depth) };
    }
    return node;
  }
  function unary(depth) {
    const tok = peek();
    if (tok && (tok.t === "-" || tok.t === "+")) {
      at += 1;
      if (depth > MAX_DEPTH) throw new SyntaxError("too deep");
      return { op: tok.t === "-" ? "neg" : "pos", a: unary(depth + 1) };
    }
    return primary(depth);
  }
  function primary(depth) {
    const tok = peek();
    if (!tok) throw new SyntaxError("unexpected end");
    if (tok.t === "num") {
      at += 1;
      return { num: tok.v };
    }
    if (tok.t === "name") {
      at += 1;
      return { name: tok.v };
    }
    if (tok.t === "(") {
      at += 1;
      const node = expr(depth + 1);
      take(")");
      return node;
    }
    if (tok.t === "[") {
      at += 1;
      const items = [expr(depth + 1)];
      while (peek() && peek().t === ",") {
        at += 1;
        items.push(expr(depth + 1));
      }
      take("]");
      return { list: items };
    }
    throw new SyntaxError(`unexpected ${tok.t}`);
  }
  const root = expr(0);
  if (at !== tokens.length) throw new SyntaxError("trailing input");
  return root;
}

function evaluate(node, env) {
  if ("num" in node) return node.num;
  if ("name" in node) return env[node.name];
  if ("list" in node) return node.list.map((item) => evaluate(item, env));
  const a = evaluate(node.a, env);
  switch (node.op) {
    case "neg":
      return -a;
    case "pos":
      return +a;
    case "+":
      return a + evaluate(node.b, env);
    case "-":
      return a - evaluate(node.b, env);
    case "*":
      return a * evaluate(node.b, env);
    case "/":
      return a / evaluate(node.b, env);
    default:
      throw new SyntaxError(`bad node ${node.op}`);
  }
}

/** The compiled size expression: (width, height, dpr) -> number | number[]. Throws on anything else. */
export function synapseExpr(source) {
  const text = String(source);
  if (text.length > MAX_SOURCE) throw new SyntaxError("expression too long");
  const tree = parse(tokenize(text));
  return function sizeExpr(width, height, dpr) {
    return evaluate(tree, { width, height, dpr });
  };
}

export default synapseExpr;
