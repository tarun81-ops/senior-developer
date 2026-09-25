// Model-written markdown, rendered safely (D27 rule 3, D29).
//
// react-markdown builds React elements; it never sets innerHTML. On top of
// that, three rules:
//   * raw HTML in the markdown is dropped (`skipHtml`, and no rehype-raw);
//   * only an allowlist of plain text elements is rendered; anything else
//     is unwrapped to its text;
//   * links and images become plain text showing their target, so nothing
//     at an approval gate is clickable or loads anything.
// Plain JS (no JSX) so Node's test runner renders it without a build step.
import { createElement as h } from "react";
import Markdown from "react-markdown";

const ALLOWED = [
  "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
  "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong", "del",
  "a", "img", // allowed only so they reach the text-only replacements below
];

const components = {
  a: ({ children, href }) =>
    h("span", { className: "md-link" }, children, href ? h("span", { className: "md-url" }, ` <${href}>`) : null),
  img: ({ alt, src }) => h("span", { className: "md-image" }, `[image: ${alt || "no description"}${src ? ` <${src}>` : ""}]`),
};

// Keep every URL exactly as written, so the reader sees the real target.
// Nothing uses it as a URL: the replacements above only print it as text.
const showUrlAsWritten = (url) => url;

export default function SafeMarkdown({ text }) {
  return h(
    "div",
    { className: "markdown" },
    h(Markdown, {
      skipHtml: true,
      allowedElements: ALLOWED,
      unwrapDisallowed: true,
      components,
      urlTransform: showUrlAsWritten,
    }, text || ""),
  );
}
