/**
 * Steam's packed per-device compat bitfield.
 *
 * The bug this guards: we wrote bits 0-1 (Deck) and nothing else, so on
 * a Steam Machine — which reads bits 6-7 — every Unifideck shortcut was
 * Unknown, and vanished from Steam's own compat filter and tile badges.
 */
import { describe, it, expect } from "vitest";
import {
  packCompat,
  overviewCompatCategory,
  PACKED_SHIFTS,
  type CompatTrack,
} from "./compat-packed";

const TRACKS = Object.keys(PACKED_SHIFTS) as CompatTrack[];

describe("packCompat", () => {
  it.each(TRACKS)("puts %s in its own bits", (track) => {
    const packed = packCompat(0, { [track]: 3 });
    expect(packed).toBe(3 << PACKED_SHIFTS[track]);
    expect(overviewCompatCategory({ steam_hw_compat_category_packed: packed }, track)).toBe(3);
  });

  it("writes every track in one pass without collision", () => {
    const packed = packCompat(0, {
      deck: 3,
      steamos: 2,
      machine: 1,
      frame: 2,
    });
    const read = (t: CompatTrack) =>
      overviewCompatCategory({ steam_hw_compat_category_packed: packed }, t);
    expect(read("deck")).toBe(3);
    expect(read("steamos")).toBe(2);
    expect(read("machine")).toBe(1);
    expect(read("frame")).toBe(2);
  });

  it("leaves the other tracks' bits untouched", () => {
    // Steam already knows this game is Verified on a Machine.
    const existing = packCompat(0, { machine: 3 });
    const after = packCompat(existing, { deck: 2 });
    expect(overviewCompatCategory({ steam_hw_compat_category_packed: after }, "machine")).toBe(3);
    expect(overviewCompatCategory({ steam_hw_compat_category_packed: after }, "deck")).toBe(2);
  });

  it("skips unknown rather than clobbering a real value", () => {
    // 0 means "we have nothing to say", not "rated Unknown". Writing it
    // would hide the game from that device's filter.
    const existing = packCompat(0, { machine: 3 });
    for (const value of [0, undefined, NaN as unknown as number]) {
      const after = packCompat(existing, { machine: value });
      expect(overviewCompatCategory({ steam_hw_compat_category_packed: after }, "machine")).toBe(3);
    }
  });

  it("overwrites a track it does have a value for", () => {
    const existing = packCompat(0, { deck: 1 });
    const after = packCompat(existing, { deck: 3 });
    expect(overviewCompatCategory({ steam_hw_compat_category_packed: after }, "deck")).toBe(3);
  });

  it("matches the live client's shift layout", () => {
    // Read off Steam's own AppOverview getters. Changing these silently
    // writes into the wrong device's slot.
    expect(PACKED_SHIFTS).toEqual({
      deck: 0,
      steamos: 4,
      machine: 6,
      frame: 8,
    });
  });

  it("stays an unsigned 32-bit value", () => {
    expect(packCompat(0xffffffff, { deck: 2 })).toBeGreaterThan(0);
    expect(Number.isInteger(packCompat(0xffffffff, { deck: 2 }))).toBe(true);
  });
});

describe("overviewCompatCategory", () => {
  it("reads 0 from a missing or empty overview", () => {
    expect(overviewCompatCategory(null, "deck")).toBe(0);
    expect(overviewCompatCategory(undefined, "machine")).toBe(0);
    expect(overviewCompatCategory({}, "machine")).toBe(0);
  });
});
