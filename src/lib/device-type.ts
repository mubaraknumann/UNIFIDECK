/**
 * Which Valve device this is, for labelling the compatibility tab.
 *
 * The backend answers from DMI (`utils/device.py`) because nothing
 * reachable from the frontend discriminates a Deck from a Steam
 * Machine: both run SteamOS, and Steam launches with the same
 * `-steamdeck -steamos3` flags on either.
 *
 * Cached module-level after the single startup fetch. The value cannot
 * change without a reboot, so re-asking would only add a round trip.
 *
 * The default is `"other"`: a backend that never answers degrades to
 * the neutral SteamOS wording rather than to a confident claim about
 * hardware nobody verified. Defaulting to `"deck"` would mean a Steam
 * Machine whose RPC failed tells its owner their games are "Great on
 * Deck" — a wrong device name, which is the exact failure this module
 * exists to prevent.
 */
import { call } from "@decky/api";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import type { CompatTrack } from "./steam-bridge/compat-packed";

export type DeviceType = "deck" | "machine" | "other";

/** The `data` payload of `get_device_type`, envelope already stripped. */
interface DeviceTypePayload {
  device_type: DeviceType;
}

const VALID: readonly DeviceType[] = ["deck", "machine", "other"];

let deviceType: DeviceType = "other";

export function getDeviceType(): DeviceType {
  return deviceType;
}

/**
 * i18n key for the compatibility label, named after the actual device.
 *
 * Lives here rather than in the tab module because it is a property of
 * the device, not of the tab: the library view, the collection names
 * and the tab title all need it, and a view should not import the tab
 * container just to get a label.
 *
 * Non-Valve hardware gets the neutral rating name rather than being
 * told its games are great on a handheld it does not own.
 */
export function compatTabTitleKey(): string {
  switch (getDeviceType()) {
    case "machine":
      return "deckTabs.greatOnMachine";
    case "other":
      return "deckTabs.steamOSCompatible";
    default:
      return "deckTabs.greatOnDeck";
  }
}

/**
 * Which of Valve's rating tracks describes this device.
 *
 * Needed only where we read Steam's *own* packed compat bitfield for a
 * native Steam app — our own shortcuts arrive with the backend's
 * already-resolved `compat_category`, so nothing else in the frontend
 * branches on device for compatibility.
 */
export function activeCompatTrack(): CompatTrack {
  switch (getDeviceType()) {
    case "machine":
      return "machine";
    case "other":
      return "steamos";
    default:
      return "deck";
  }
}

/**
 * Every key `compatTabTitleKey` can return.
 *
 * Collections are account-global and cloud-synced, so a device must
 * recognise the *other* devices' compat-collection names as valid or it
 * deletes them on every boot. Exported beside the switch above so the
 * two cannot drift apart.
 */
export const COMPAT_TAB_TITLE_KEYS: readonly string[] = [
  "deckTabs.greatOnDeck",
  "deckTabs.greatOnMachine",
  "deckTabs.steamOSCompatible",
];

/** Test seam — reset the cache between cases. */
export function __setDeviceTypeForTests(value: DeviceType): void {
  deviceType = value;
}

/**
 * Fetch the device type once and cache it.
 *
 * @returns true if the cached value actually changed, so the caller
 *   can decide whether a re-render is warranted rather than firing one
 *   unconditionally on every boot.
 */
export async function loadDeviceType(): Promise<boolean> {
  try {
    const raw = await call<[], unknown>(rpcRoutes.getDeviceType);
    const r = unwrapRpcEnvelope<DeviceTypePayload>(raw, {
      route: rpcRoutes.getDeviceType,
      throwing: false,
    });
    const next = r?.device_type;
    // Validate rather than trust: an older backend answers this route
    // with an error envelope, and assigning undefined here would blank
    // the tab title instead of leaving the default in place.
    if (!next || !VALID.includes(next)) return false;
    if (next === deviceType) return false;
    deviceType = next;
    return true;
  } catch {
    // Backend not ready — the neutral "other" default stays.
    return false;
  }
}
