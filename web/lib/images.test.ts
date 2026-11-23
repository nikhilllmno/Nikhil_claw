import { describe, expect, it } from "vitest";
import { addImages, imageFiles, withoutImageMarkers } from "./images";

const file = (name: string, type: string) => new File([new Uint8Array(2)], name, { type });

describe("images", () => {
  it("drops the marker lines of the API", () => {
    const text = "look at this\n[image attached: a1 shot.png; use vision_analyze to look at it]";
    expect(withoutImageMarkers(text)).toBe("look at this");
    expect(withoutImageMarkers("[image attached: a1 x.png; use vision_analyze to look at it]")).toBe("");
    expect(withoutImageMarkers("no [image attached: here")).toBe("no [image attached: here");
  });

  it("keeps only images, and at most four", () => {
    expect(imageFiles([file("a.png", "image/png"), file("b.txt", "text/plain")]).map((f) => f.name)).toEqual(["a.png"]);
    const five = Array.from({ length: 5 }, (_, i) => file(`${i}.png`, "image/png"));
    const { files, dropped } = addImages(five.slice(0, 3), five.slice(3));
    expect(files).toHaveLength(4);
    expect(dropped).toBe(1);
  });
});
