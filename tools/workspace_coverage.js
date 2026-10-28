// Only this exact generated notice is shortened. Authored warnings, findings
// and page citations are left alone; the underlying Markdown is never edited.
const pageList = "(?:not recorded|[1-9][0-9]*(?:, [1-9][0-9]*)*)";
const generated = new RegExp(`^\\[!warning\\] Coverage boundary\\nRecovered PDF pages: ${pageList}\\. Missing or unresolved PDF pages: ${pageList}\\. Claims in this note are restricted to the recovered pages and must not be treated as findings from the complete document\\.$`);

export function compactCoverageNotice(text) {
  if (typeof text !== "string") return null;
  const content = text.replaceAll("\r\n", "\n").replace(/^> ?/gm, "").trim();
  return generated.test(content) ? "This note is based on an incomplete extraction of the document." : null;
}
