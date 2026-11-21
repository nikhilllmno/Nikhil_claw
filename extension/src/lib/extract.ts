// browser_extract in the user's Chrome. The functions run in the isolated world of the tab
// (Runtime.callFunctionOn with an element as `this`). They must not use anything from outside the
// function. The limits are the limits of the cloud browser (docs/architecture.md, Section 14.1).

/** Reads the text, the links, or the tables of an element. Returns JSON, or plain text. */
export function extractFrom(this: Element, kind: string): string {
  const root = this as HTMLElement;
  const clean = (text: string) => text.replace(/\s+/g, " ").trim();
  if (kind === "text") {
    const text = (root.innerText ?? root.textContent ?? "").replace(/\n{3,}/g, "\n\n").trim();
    return text.length > 100_000 ? `${text.slice(0, 100_000)}\n[cut at 100000 characters]` : text;
  }
  if (kind === "links") {
    const links = [...root.querySelectorAll("a[href]")].slice(0, 500).map((element) => {
      const anchor = element as HTMLAnchorElement;
      return { text: clean(anchor.innerText || anchor.getAttribute("aria-label") || "").slice(0, 300), href: anchor.href };
    });
    return JSON.stringify(links);
  }
  const tables = [...root.querySelectorAll("table")].slice(0, 20).map((table) =>
    [...table.querySelectorAll("tr")]
      .slice(0, 200)
      .map((row) => [...row.querySelectorAll("th, td")].map((cell) => clean((cell as HTMLElement).innerText ?? "").slice(0, 500))),
  );
  return JSON.stringify(tables);
}

/** The value or the text of one element. A password field gives "(hidden)". */
export function fieldValue(this: Element): string {
  const element = this as HTMLInputElement;
  if (element.tagName === "INPUT" && (element.type || "").toLowerCase() === "password") return "(hidden)";
  if ("value" in element && typeof element.value === "string" && ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName)) {
    return element.value.slice(0, 10_000);
  }
  return ((element as unknown as HTMLElement).innerText ?? element.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 10_000);
}

/** Selects the options of a `select` element by value or by label. Returns the selected values. */
export function selectOptions(this: Element, values: string[]): string[] {
  const select = this as HTMLSelectElement;
  if (select.tagName !== "SELECT") throw new Error("the element is not a select element");
  const wanted = new Set(values);
  const picked: string[] = [];
  for (const option of [...select.options]) {
    const match = wanted.has(option.value) || wanted.has(option.label) || wanted.has(option.text.trim());
    option.selected = match && (select.multiple || picked.length === 0);
    if (option.selected) picked.push(option.value);
  }
  if (picked.length === 0) throw new Error("no option has these values or labels");
  select.dispatchEvent(new Event("input", { bubbles: true }));
  select.dispatchEvent(new Event("change", { bubbles: true }));
  return picked;
}

/** Selects the text of a field, so that the next typed text replaces it. */
export function selectAllText(this: Element): void {
  const element = this as HTMLInputElement;
  if (typeof element.select === "function") {
    element.select();
    return;
  }
  if ((element as unknown as HTMLElement).isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(element);
    const selection = getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  }
}
