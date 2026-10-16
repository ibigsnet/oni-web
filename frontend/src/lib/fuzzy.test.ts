import { describe, expect, it } from "vitest";
import { matches, rank } from "./fuzzy";

describe("fuzzy search", () => {
  it("forgives typos", () => {
    expect(matches("gluch", "Huge Gulch V90")).toBe(true);
    expect(matches("valhala", "Valhalla")).toBe(true);
    expect(matches("highground", "High Ground")).toBe(true);
    expect(matches("valhal", "Valhalla")).toBe(true);
  });
  it("stays strict where it should", () => {
    expect(matches("gluch", "Guardian")).toBe(false);
    expect(matches("pit", "Pot")).toBe(false);
    expect(matches("gulch zzzz", "Huge Gulch")).toBe(false);
  });
  it("ranks exact first", () => {
    expect(rank("gulch", ["Gluch Arena", "Huge Gulch", "Guardian"], (x) => [x])).toEqual(["Huge Gulch", "Gluch Arena"]);
  });
});
