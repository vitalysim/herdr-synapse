// docs.js: chart doc assets by content-addressed name, fetched once.
import { beforeEach, describe, expect, test, vi } from "vitest";
import { optionFixtures } from "./__fixtures__/fixtures.js";

vi.mock("../../api.js", () => ({ teamPath: (team, rest) => `/api/teams/${team}/${rest}`, getJSON: vi.fn(async (url) => ({ url })) }));
const { getJSON } = await import("../../api.js");
const { docNameOf, loadDoc } = await import("./docs.js");

const NAME = "0123456789abcdef0123456789abcdef.json";
beforeEach(() => vi.mocked(getJSON).mockClear());

describe("loadDoc", () => {
  test("fetches the team's asset once per name", async () => {
    expect(await loadDoc("t 1", NAME)).toEqual({ url: `/api/teams/t 1/assets/${NAME}` });
    await loadDoc("t 1", NAME);
    expect(getJSON).toHaveBeenCalledTimes(1);
  });
  test("no name is no doc; a malformed name is refused before any fetch", async () => {
    expect(await loadDoc("t", null)).toBe(null);
    await expect(loadDoc("t", "../secret.json")).rejects.toThrow(/not a doc asset name/);
    await expect(loadDoc("t", "abc.glb")).rejects.toThrow();
    expect(getJSON).not.toHaveBeenCalled();
  });
  test("the slot's ref.doc wins over the element's doc_asset", () => {
    expect(docNameOf({ ref: { doc: "a" } }, { doc_asset: "b" })).toBe("a");
    expect(docNameOf({}, { doc_asset: "b" })).toBe("b");
    expect(docNameOf(null, null)).toBe(null);
  });
  test("every fixture names a well-formed doc", () => {
    for (const f of optionFixtures()) if (f.element.doc_asset) expect(f.element.doc_asset, f.name).toMatch(/^[0-9a-f]{32}\.json$/);
  });
});
