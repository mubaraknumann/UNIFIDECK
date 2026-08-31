// @vitest-environment jsdom
/**
 * The compatibility tab is titled after the hardware it filters for.
 *
 * Three things must hold: a Steam Machine owner is never told their
 * games are "Great on Deck"; a backend that cannot answer leaves the
 * cached value rather than blanking the tab; and the *unanswered*
 * default is the neutral SteamOS label, not "deck". That last one
 * would ship silently broken, because on the dev Deck a wrong default
 * and the correct answer are the same string.
 *
 * Also covers the hide-list: Steam names its own compat tab after the
 * device too, so we must hide every id it might emit.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@decky/ui", () => ({ gamepadTabbedPageClasses: undefined }));
vi.mock("i18next", () => ({ default: { t: (key: string) => key } }));
vi.mock("../library-filters", () => ({
  runFilters: () => true,
  setStoreCountSink: () => {},
}));
vi.mock("@decky/api", () => ({ call: vi.fn() }));
vi.mock("../../api/useRPC", () => ({
  unwrapRpcEnvelope: (raw: unknown) =>
    raw && typeof raw === "object" && "success" in raw ? raw : undefined,
}));

import { call } from "@decky/api";
import { getUnifideckTabs, HIDDEN_DEFAULT_TABS } from "./tab-container";
import {
  loadDeviceType,
  getDeviceType,
  __setDeviceTypeForTests,
  type DeviceType,
} from "../device-type";

const compatTabTitle = (): string =>
  getUnifideckTabs().find((t) => t.id === "unifideck-deck")!.title;

beforeEach(() => {
  vi.mocked(call).mockReset();
  __setDeviceTypeForTests("deck");
});

describe("compatibility tab title", () => {
  it.each([
    ["deck", "deckTabs.greatOnDeck"],
    ["machine", "deckTabs.greatOnMachine"],
    ["other", "deckTabs.steamOSCompatible"],
  ] as [DeviceType, string][])("titles %s as %s", (device, key) => {
    __setDeviceTypeForTests(device);
    expect(compatTabTitle()).toBe(key);
  });

  it("keeps the tab id and filter fixed so no layout moves", () => {
    __setDeviceTypeForTests("machine");
    const tab = getUnifideckTabs().find((t) => t.id === "unifideck-deck")!;
    expect(tab.position).toBe(0);
    expect(tab.filters).toEqual([{ type: "deckCompat", params: {} }]);
  });
});

describe("loadDeviceType", () => {
  it("caches a valid answer and reports the change", async () => {
    vi.mocked(call).mockResolvedValue({ success: true, device_type: "machine" });
    await expect(loadDeviceType()).resolves.toBe(true);
    expect(getDeviceType()).toBe("machine");
    expect(compatTabTitle()).toBe("deckTabs.greatOnMachine");
  });

  it("reports no change when the answer matches the default", async () => {
    vi.mocked(call).mockResolvedValue({ success: true, device_type: "deck" });
    await expect(loadDeviceType()).resolves.toBe(false);
    expect(getDeviceType()).toBe("deck");
  });

  it("ignores a value outside the known set", async () => {
    vi.mocked(call).mockResolvedValue({ success: true, device_type: "toaster" });
    await expect(loadDeviceType()).resolves.toBe(false);
    expect(getDeviceType()).toBe("deck");
  });

  it("keeps the default when an older backend has no such route", async () => {
    vi.mocked(call).mockRejectedValue(new Error("unknown method"));
    await expect(loadDeviceType()).resolves.toBe(false);
    expect(compatTabTitle()).toBe("deckTabs.greatOnDeck");
  });

  it("keeps the default when the payload omits the field", async () => {
    vi.mocked(call).mockResolvedValue({ success: true });
    await expect(loadDeviceType()).resolves.toBe(false);
    expect(getDeviceType()).toBe("deck");
  });

  it("defaults to the neutral label before anything answers", async () => {
    // A device whose RPC never lands must not claim to be a Deck: on a
    // Steam Machine that is a wrong device name, the exact failure this
    // module exists to prevent.
    vi.resetModules();
    const fresh = await import("../device-type");
    expect(fresh.getDeviceType()).toBe("other");
    expect(fresh.compatTabTitleKey()).toBe("deckTabs.steamOSCompatible");
  });
});

describe("hidden native tabs", () => {
  /**
   * Steam picks its own compat tab id from the running device:
   * GreatOnDeck / GreatOnMachine / SteamOSCompatible. Hiding only the
   * Deck id leaves a second, identically-titled tab beside ours on a
   * Steam Machine.
   */
  it.each(["GreatOnDeck", "GreatOnMachine", "SteamOSCompatible"])(
    "hides Steam's own %s tab",
    (id) => {
      expect(HIDDEN_DEFAULT_TABS).toContain(id);
    },
  );

  it("does not vary by device, so a mis-detected device still hides all", () => {
    const perDevice = (["deck", "machine", "other"] as DeviceType[]).map((device) => {
      __setDeviceTypeForTests(device);
      return [...HIDDEN_DEFAULT_TABS].sort();
    });
    expect(perDevice[1]).toEqual(perDevice[0]);
    expect(perDevice[2]).toEqual(perDevice[0]);
  });
});
