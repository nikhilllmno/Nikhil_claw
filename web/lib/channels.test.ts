import { describe, expect, it } from "vitest";
import { emailProblem, pairingHint, providerOf, whatsappBody, whatsappProblem } from "./channels";

const twilio = {
  provider: "twilio" as const,
  number: " +15550001111 ",
  api_token: " token ",
  account_sid: "AC" + "1".repeat(32),
  app_id: "left over",
};

describe("WhatsApp connect", () => {
  it("checks the number and the fields of the provider", () => {
    expect(whatsappProblem(twilio)).toBeNull();
    expect(whatsappProblem({ ...twilio, number: "15550001111" })).toMatch(/international/);
    expect(whatsappProblem({ ...twilio, api_token: " " })).toMatch(/auth token/);
    expect(whatsappProblem({ ...twilio, account_sid: "AC1" })).toMatch(/AC/);
    expect(whatsappProblem({ provider: "meta", number: "+15550001111", api_token: "t" })).toMatch(
      /phone number id/,
    );
  });

  it("sends only the fields of the chosen provider", () => {
    expect(whatsappBody(twilio)).toEqual({
      provider: "twilio",
      number: "+15550001111",
      api_token: "token",
      account_sid: "AC" + "1".repeat(32),
    });
  });

  it("reads the provider of the page link", () => {
    expect(providerOf("gupshup")).toBe("gupshup");
    expect(providerOf("other")).toBe("meta");
    expect(providerOf(null)).toBe("meta");
  });
});

describe("email connect", () => {
  it("checks the address, the key, and the public key", () => {
    const form = { address: "sama@mail.example.com", api_key: "SG.x.y", public_key: "M".repeat(40) };
    expect(emailProblem(form)).toBeNull();
    expect(emailProblem({ ...form, address: "sama+x@mail.example.com" })).toMatch(/without \+/);
    expect(emailProblem({ ...form, api_key: "key" })).toMatch(/SG\./);
    expect(emailProblem({ ...form, public_key: "short" })).toMatch(/public key/);
  });
});

it("tells where a pairing code goes", () => {
  expect(pairingHint("whatsapp").to).toMatch(/WhatsApp/);
  expect(pairingHint("email").to).toMatch(/subject/);
  expect(pairingHint("telegram").to).toBe("the bot");
});
