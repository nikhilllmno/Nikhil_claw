// The sites where sama never acts in the user's Chrome (docs/architecture.md, Section 14.2.2).
// The user cannot allow them. Keep BLOCKED_HOSTS the same as src/sama/browser/chrome.py (a test
// of the API checks it).

export const BLOCKED_HOSTS: readonly string[] = [
  // Google account and payment pages, and the Chrome Web Store.
  "accounts.google.com",
  "myaccount.google.com",
  "passwords.google.com",
  "pay.google.com",
  "payments.google.com",
  "chromewebstore.google.com",
  "chrome.google.com",
  // Password managers.
  "1password.com",
  "1password.eu",
  "1password.ca",
  "lastpass.com",
  "bitwarden.com",
  "bitwarden.eu",
  "dashlane.com",
  "keepersecurity.com",
  "keepersecurity.eu",
  "nordpass.com",
  "roboform.com",
  "proton.me",
  // Payments, wallets, and exchanges.
  "paypal.com",
  "venmo.com",
  "wise.com",
  "revolut.com",
  "paytm.com",
  "phonepe.com",
  "coinbase.com",
  "binance.com",
  "kraken.com",
  // Banks with a host name that does not have the word "bank".
  "chase.com",
  "wellsfargo.com",
  "citi.com",
  "citibank.com",
  "hsbc.com",
  "hsbc.co.uk",
  "hsbc.co.in",
  "barclays.co.uk",
  "barclays.com",
  "lloydsbank.com",
  "natwest.com",
  "santander.co.uk",
  "capitalone.com",
  "usbank.com",
  "schwab.com",
  "fidelity.com",
  "vanguard.com",
  "americanexpress.com",
  "discover.com",
  "onlinesbi.sbi",
  "sbi.co.in",
  "kotak.com",
  "icici.com",
];

/** A host with a label that has this word is a bank site. */
export const BLOCKED_HOST_WORD = "bank";

/** The host name of a URL in lower case, or `null`. */
export function hostOf(url: string): string | null {
  try {
    const host = new URL(url).hostname.toLowerCase().replace(/\.$/, "");
    return host || null;
  } catch {
    return null;
  }
}

/** True when sama must not act on the page of `url`. */
export function isBlockedSite(url: string): boolean {
  const host = hostOf(url);
  if (host === null) return false;
  if (BLOCKED_HOSTS.some((blocked) => host === blocked || host.endsWith(`.${blocked}`))) return true;
  return host.split(".").some((label) => label.includes(BLOCKED_HOST_WORD));
}

/** True for a page where sama can act: `http` or `https`. */
export function isWebPage(url: string | undefined): boolean {
  if (!url) return false;
  try {
    const { protocol } = new URL(url);
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}
