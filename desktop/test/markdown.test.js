// node --test: model markdown at an approval gate can't inject markup, script
// or anything clickable (D27 rule 3, D29).
import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import SafeMarkdown from "../src/SafeMarkdown.js";

const render = (text) => renderToStaticMarkup(createElement(SafeMarkdown, { text }));

/** No tag in the output may be anything but the allowlisted plain elements. */
const TAG = /<\s*([a-zA-Z0-9-]+)([^>]*)>/g;
const ALLOWED_TAGS = new Set([
  "div", "span", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
  "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong", "del",
]);

function assertInert(html) {
  for (const [, tag, attrs] of html.matchAll(TAG)) {
    assert.ok(ALLOWED_TAGS.has(tag.toLowerCase()), `tag <${tag}> rendered: ${html}`);
    // the only attribute we ever emit is our own class name
    const names = [...attrs.matchAll(/([a-zA-Z-:]+)=/g)].map((m) => m[1]);
    assert.ok(names.every((n) => n === "class"), `attribute ${names} rendered: ${html}`);
  }
}

const HOSTILE = {
  "script tag": "<script>alert(1)</script>",
  "img onerror": '<img src=x onerror="alert(1)">',
  "raw anchor": '<a href="https://evil.example">Approve here</a>',
  "iframe": '<iframe src="https://evil.example"></iframe>',
  "style tag": "<style>button{display:none}</style>",
  "svg onload": '<svg onload="alert(1)"></svg>',
  "javascript link": "[click me](javascript:alert(1))",
  "https link": "[the docs](https://evil.example/docs)",
  "autolink": "<https://evil.example>",
  "bare url": "see https://evil.example now",
  "reference link": "[ref][1]\n\n[1]: https://evil.example",
  "markdown image": "![logo](https://evil.example/x.png)",
  "data image": "![fake approve button](data:image/png;base64,iVBORw0KGgo=)",
  "html comment": "<!-- <script>alert(1)</script> -->",
  "entity-encoded script": "&lt;script&gt;alert(1)&lt;/script&gt;",
};

for (const [name, text] of Object.entries(HOSTILE)) {
  test(`hostile markdown is inert: ${name}`, () => {
    const html = render(text);
    assertInert(html);
    assert.ok(!/<(a|img|script|iframe|style|svg)\b/i.test(html), html);
    assert.ok(!/\b(href|src|on[a-z]+)=/i.test(html), html);
  });
}

test("link and image targets are shown as text, exactly as written", () => {
  assert.match(render("[the docs](https://evil.example/docs)"), /the docs<span class="md-url"> &lt;https:\/\/evil\.example\/docs&gt;<\/span>/);
  assert.match(render("[click me](javascript:alert(1))"), /click me<span class="md-url"> &lt;javascript:alert\(1\)&gt;/);
  assert.match(render("![logo](https://evil.example/x.png)"), /\[image: logo &lt;https:\/\/evil\.example\/x\.png&gt;\]/);
});

test("ordinary plan markdown still renders as structure", () => {
  const html = render("# Plan\n\n1. **Parse** input\n2. Add `tests`\n\n```py\nprint(1)\n```\n\n> note");
  assertInert(html);
  for (const tag of ["<h1>", "<ol>", "<li>", "<strong>", "<code>", "<pre>", "<blockquote>"]) {
    assert.ok(html.includes(tag), `${tag} missing in ${html}`);
  }
});

test("empty output renders nothing, not an error", () => {
  assert.equal(render(""), '<div class="markdown"></div>');
});
