/**
 * usePlaySection — Steam "Play / Install" button override.
 *
 * Mounted by `<PlaySectionWrapper>`, this hook decides what
 * the Play section should look like for a given Steam appId :
 *
 *  - Steam-native game → no override (returns
 *    `{ shouldOverride: false }`)
 *  - Unifideck game, NOT installed → expose `installAction`
 *  - Unifideck game, IN download queue → expose
 *    `cancelDownloadAction` + the live download item
 *  - Unifideck game, installed → expose `playAction` plus
 *    `uninstallAction`
 *
 * The decision is recomputed reactively when the underlying
 * `useGameInfo` data or the download queue snapshot changes.
 * The render component reads `shouldOverride` and the
 * action functions; it stays presentational.
 */
import { useMemo } from "react";
import { useGameInfo } from "./useGameInfo";
import { useDownloads } from "../contexts/DownloadContext";
import { authStore } from "../stores/auth-store";
import type { DownloadItem } from "../types/downloads";
import type { StoreStatus } from "../types/api";

/**
 * Discriminated union of the Play-section states. Drives
 * which variant of the Play button is rendered
 * (NotInstalled / Downloading / Installed / Launching).
 */
export type PlaySectionState =
  | { kind: "steam-native"; shouldOverride: false }
  | { kind: "not-installed"; shouldOverride: true; installable: true }
  | { kind: "downloading"; shouldOverride: true; download: DownloadItem }
  | { kind: "installed"; shouldOverride: true; appId: number }
  | {
      kind: "browser";
      shouldOverride: true;
      appId: number;
      /** "stream" = xCloud (Play on Cloud); "web" = an HTML5 game. */
      variant: "stream" | "web";
      url: string;
      /** When set, the browser game is a connection-gated store (Amazon
       *  Luna, Req 10): Play is enabled only while `authStatus === "connected"`,
       *  disabled with a sign-in prompt while `"disconnected"`, and shown with
       *  a re-authentication prompt while `"expired"` (session expired). xCloud
       *  and itch.io leave this undefined and stay always-playable. */
      gatedStore?: "luna";
      /** The gated store's current connection status, read from the shared
       *  auth-store. Undefined until the first `check_store_status` lands. */
      authStatus?: StoreStatus;
    };

/**
 * Hook that resolves the current Play-section state
 * for an app : not-installed, downloading, installed,
 * launching, or running. Watches install / launch /
 * download events to recompute on every transition,
 * driving the right Play-button variant.
 *
 * @param appId — Steam shortcut app-id.
 * @returns current `PlaySectionState`.
 */
export function usePlaySection(appId: number | null): PlaySectionState {
  const info = useGameInfo(appId);
  const downloads = useDownloads();
  // Luna's Play control is gated on its connection state (Req 10), so the
  // section needs the live Luna status. Read it straight from the boot-time
  // auth-store SINGLETON — NOT via useAuth()/AuthContext. This hook renders
  // in Steam's injected React tree, which is not under <AuthProvider>, so
  // useAuth() threw "called outside <AuthProvider>" there and crashed the
  // whole Play section for EVERY store (observed on-device). A plain store
  // read has no provider dependency and no extra React-hook requirement; the
  // Play section re-mounts on store-status changes, so this stays current.
  const lunaStatus = authStore.getSnapshot().statuses.luna;

  return useMemo<PlaySectionState>(() => {
    if (appId == null || !info.data) {
      return { kind: "steam-native", shouldOverride: false };
    }

    const game = info.data;
    // Filter out games whose store is plain Steam — we never override those.
    if (game.store === "steam") {
      return { kind: "steam-native", shouldOverride: false };
    }

    // Browser games (xCloud streams, itch.io HTML5 games) open in an Edge
    // window and install nothing, so they are always playable: surface a
    // Play state regardless of is_installed. The launcher finds the URL in
    // the library cache (backend `launcher/browser_games`). `xcloud` alone
    // still counts, for a library cached before the `browser` tag existed.
    const tags = game.store_tags ?? [];
    // A Luna title is a browser-session stream like xCloud, but its Play
    // control is connection-gated (Req 10). Detect it by the `luna` store tag
    // or the store id so a library cached before the tag existed still gates.
    const isLuna = game.store === "luna" || tags.includes("luna");
    if (tags.includes("browser") || tags.includes("xcloud") || isLuna) {
      // Luna is always a stream; xCloud is a stream; itch.io is `web`.
      const stream = isLuna || tags.includes("xcloud");
      return {
        kind: "browser",
        shouldOverride: true,
        appId,
        variant: stream ? "stream" : "web",
        url:
          game.browser_url ??
          (stream && !isLuna
            ? `https://www.xbox.com/play/launch/${game.id}`
            : ""),
        ...(isLuna ? { gatedStore: "luna", authStatus: lunaStatus } : {}),
      };
    }

    // Check the live queue first — a download in progress
    // takes precedence over `is_installed` flag staleness.
    const inQueue =
      findInQueue(downloads.queue?.current, game.id) ??
      downloads.queue?.queued.find(
        (d) => d.game_id === game.id && !isTerminal(d),
      ) ??
      null;
    if (inQueue) {
      return { kind: "downloading", shouldOverride: true, download: inQueue };
    }
    if (!game.is_installed) {
      return { kind: "not-installed", shouldOverride: true, installable: true };
    }

    return { kind: "installed", shouldOverride: true, appId };
  }, [appId, info.data, downloads.queue, lunaStatus]);
}

/**
 * Statuses meaning the item is finished with. A finished download must never
 * render as an active one: the "downloading" branch above deliberately
 * outranks `is_installed`, so a stale row does not just look wrong, it hides
 * the Play button behind a progress bar whose Cancel cannot succeed.
 *
 * The backend filters these out of `current` too (`get_queue`). This is the
 * second line of defence, and it is worth having: the queue snapshot is only
 * replaced when a download event arrives, there is no polling refetch, and the
 * event that clears a row is the same one that races the backend's own
 * cleanup.
 */
const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "complete",
  "failed",
  "cancelled",
]);

function isTerminal(item: DownloadItem): boolean {
  return TERMINAL_STATUSES.has(item.status);
}

function findInQueue(
  current: DownloadItem | null | undefined,
  gameId: string,
): DownloadItem | null {
  if (!current || isTerminal(current)) return null;

  return current.game_id === gameId ? current : null;
}
