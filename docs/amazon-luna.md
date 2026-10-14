# Amazon Luna — Technical Documentation

## Table of contents

- [Overview](#overview)
  - [What it brings to the plugin](#what-it-brings-to-the-plugin)
  - [How Luna differs from xCloud](#how-luna-differs-from-xcloud)
- [Architecture](#architecture)
  - [Files and responsibilities](#files-and-responsibilities)
  - [Two processes](#two-processes)
- [Authentication flow](#authentication-flow)
  - [Browser-session sign-in](#browser-session-sign-in)
  - [Session persistence and expiry](#session-persistence-and-expiry)
  - [Browser installation](#browser-installation)
- [Platform spoofing (required)](#platform-spoofing-required)
  - [Why the spoofer is mandatory](#why-the-spoofer-is-mandatory)
  - [What the spoofer overrides](#what-the-spoofer-overrides)
  - [Version tracking](#version-tracking)
  - [Verification and failure](#verification-and-failure)
- [Catalog synchronization](#catalog-synchronization)
  - [How the launcher knows it is Luna](#how-the-launcher-knows-it-is-luna)
  - [Subscription and entitlement states](#subscription-and-entitlement-states)
- [Launching a Luna title](#launching-a-luna-title)
  - [Launch flow](#launch-flow)
  - [Browser flags for Luna](#browser-flags-for-luna)
  - [Dedicated Luna profile (no SSO)](#dedicated-luna-profile-no-sso)
- [Controller support (Steam Deck)](#controller-support-steam-deck)
  - [Supported target environment](#supported-target-environment)
  - [Manual layout selection](#manual-layout-selection)
- [Virtual keyboard](#virtual-keyboard)
- [Steam Deck environment](#steam-deck-environment)
- [Logout and profile handling](#logout-and-profile-handling)
- [Error handling](#error-handling)
- [Prerequisites and dependencies](#prerequisites-and-dependencies)
- [Configuration](#configuration)
- [Internationalization](#internationalization)
- [Security](#security)
- [Known limitations](#known-limitations)

---

## Overview

The Amazon Luna integration lets Steam Deck users **stream their Luna games** directly from their Steam library through the Unifideck plugin. Games are streamed from Amazon servers — nothing is installed locally.

Luna docks onto the generic **browser-games infrastructure** introduced in Unifideck 0.7.7: a Luna title is an ordinary browser game of kind `stream`, routed through `launcher/browser_games.py` and `flows/browser_window.py` reading `library_cache.json`. It reuses the shared browser process layer, the CDP transport, the flatpak installer, the per-flavour CDP ports, and the CDP-injected virtual keyboard that the xCloud (`stream`) path already uses.

The one capability 0.7.7 does not provide — and which a hands-on feasibility test on a real Steam Deck proved mandatory — is a **CDP platform-spoofing layer**. xCloud is also a `stream` browser game and needs no spoof; Luna does, because Luna actively blocks Linux at game start. That spoofing layer is the Luna-specific addition on top of the shared path.

### What it brings to the plugin

- **Access to your Luna catalog** without downloading — titles stream in a fullscreen kiosk browser
- **Browser-session sign-in**: a single interactive Amazon sign-in from Steam's QAM (Quick Access Menu)
- **Native launch**: Luna games appear in the Steam library as shortcuts, with artwork and a Play control
- **Virtual keyboard** (AZERTY/QWERTY) injected via CDP for touchscreen text entry
- **Controller support in Gaming Mode** with a gamepad Steam Input layout

### How Luna differs from xCloud

Luna is architecturally heavier than the Microsoft / xCloud integration in three ways, all verified on a real Steam Deck:

1. **Auth is browser-session-cookie only, not OAuth.** There is no client id, no authorize/token endpoint, no refresh token, and no token file. "Signed in" means _non-expired Amazon cookies exist in the Luna browser profile_. The xCloud token manager is replaced by a `LunaSessionMonitor` that reads that cookie store.
2. **Luna blocks Linux at game start** with "Nicht unterstütztes Betriebssystem" ("Unsupported operating system"), and the block is only bypassable with the persistent platform spoofer described below.
3. **Luna has its own browser profile**, separate from the `edge-auth` profile used by the other store integrations, so there is **no single sign-on** with those stores. Luna needs its own interactive sign-in.

Luna is also separate from the download-based Amazon Games (Prime Gaming / Nile) integration under `stores/amazon/`. Those credentials and that profile are not shared with Luna.

---

## Architecture

### Files and responsibilities

The Luna integration is a package under `stores/luna/`. It mirrors `stores/microsoft/` but has **no `tokens/` package** (Luna has no tokens); the session monitor and the spoofer/play-activator take over that role.

| File                                           | Role                                                                      |
| ---------------------------------------------- | ------------------------------------------------------------------------- |
| `stores/luna/luna_store.py`                    | Main connector: auth orchestration, catalog, region, status, retry        |
| `stores/luna/luna_config.py`                   | Typed view over `stores.luna` config (no OAuth keys)                      |
| `stores/luna/luna_auth.py`                     | Drives the interactive Amazon sign-in                                     |
| `stores/luna/luna_browser_auth.py`            | Observes sign-in over CDP, confirms cookies persisted                     |
| `stores/luna/luna_session_monitor.py`         | Cookie-store read + logout, replaces the token manager                    |
| `stores/luna/luna_platform_spoofer.py`        | Browser-level CDP OS-identity override (the required spoofer)             |
| `stores/luna/luna_play_activator.py`          | CDP play-trigger → `/player` → `<video>` playing confirmation             |
| `stores/luna/luna_catalog.py`                 | Catalog models, DOM-scrape source, library-cache reconciliation           |
| `stores/luna/luna_subscription.py`            | Entitlement determination and Req 3 outcome mapping                       |
| `stores/luna/luna_browser.py`                 | Thin config over the reused `EdgeBrowser` process layer                   |
| `stores/luna/luna_profile_override.py`        | Luna profile dir + narrowly scoped flatpak filesystem override            |
| `stores/luna/luna_keyboard.py`                | Reused CDP page-inject virtual keyboard                                   |
| `stores/luna/luna_launch_flow.py`             | Launch orchestration on the 0.7.7 browser-games path                      |
| `auth/edge_browser/`                           | Browser install + launch + profile model (shared)                        |
| `launcher/browser_games.py`                    | Resolves a browser game to a `BrowserTarget` (shared with xCloud/itch.io) |
| `services/launcher/browser_game.py`            | The kiosk launch, held open until the window closes (shared)              |

### Two processes

Like xCloud, Luna runs in two OS process contexts, and the boundary matters:

- **Plugin backend** (long-lived, inside PluginLoader): owns `LunaStore` and all connect/sync/logout/status logic. This is where the connection state is computed and where Luna browser-game library entries are written into `library_cache.json`.
- **Launcher subprocess** (short-lived, spawned by Steam when you press Play): resolves the title through the browser-games path, opens the kiosk browser, and runs the Luna-specific sequence (spoofer → verify → play-activation → keyboard). The spoofer and play-activator live here because they attach to the browser the launcher just spawned.

The two share only files on disk: config, `library_cache.json`, the Luna browser profile (cookies), and the launcher log. Both the spoofer and the play-activator are constructible from `LunaConfig` alone, with no dependency on `LunaStore`, so either process can build them.

---

## Authentication flow

### Browser-session sign-in

Authentication is **browser-session-cookie based**. There is no OAuth code or token exchange. `LunaAuth` opens the kiosk browser at the configured Luna sign-in page with the platform spoofer already attached; `LunaBrowserAuth` observes the signed-in state over CDP; and `LunaSessionMonitor` confirms Amazon session cookies landed in the Luna profile.

The connector polls `LunaSessionMonitor.is_usable()` until cookies appear or the **300-second** sign-in timeout fires. If the session is closed early, no cookies persisted, or the timeout fires, any partial state is discarded and the panel returns to not-connected.

### Session persistence and expiry

`LunaSessionMonitor` is the single source of truth for "is the Luna session usable?". Because the browser locks its SQLite cookie database, it reads via a temporary copy:

```python
shutil.copy2(cookie_db, tmp_path)   # read without the browser's lock
# usable iff ≥1 Amazon session cookie is present AND its expiry is in the future
```

The read must complete within **5 seconds**. The outcomes:

- **usable** — at least one non-expired Amazon cookie → connected
- **expired** — cookies absent or all expired → session-expired (a re-authentication prompt, distinct from never-connected)
- **unknown** — the store could not be read in time → session-unknown, the profile is left untouched, and a retry is exposed

An expiry mid-launch is additionally caught by Luna presenting a sign-in page; the monitor exposes a `looks_like_signin_page(cdp)` check the launch flow uses.

### Browser installation

Luna's prerequisite is a usable flatpak browser (the whole feature rides the CDP spoof, so without the browser nothing works). When no supported browser is detected, the backend reports `needs_browser` and the frontend offers to install it via flatpak. On a successful install, the Luna profile-dir and `/run/udev:ro` overrides are applied and the interrupted operation resumes without further user intervention.

---

## Platform spoofing (required)

### Why the spoofer is mandatory

**Luna will not stream on Linux without the spoofer.** The feasibility test confirmed Luna blocks the client at game start with "Nicht unterstütztes Betriebssystem" ("Unsupported operating system"). The decisive signal Luna reads is `navigator.userAgentData.getHighEntropyValues().platform` (User-Agent Client Hints) — **not** the User-Agent string alone. Overriding only the UA string is not enough.

`LunaPlatformSpoofer` runs as a **persistent CDP session for the whole streaming session**, attached at **browser** level (not page level). It installs a browser-level `Target.setAutoAttach { autoAttach, flatten, waitForDebuggerOnStart }`, so every target — current, future, pages, and service workers — is caught, and applies the full override to each target **before** releasing it from waiting-for-debugger.

Browser-level attach is required because a page-scoped override dies on navigation to the game page: the old target is gone and its override with it. This was observed as a real failure mode. Browser-level auto-attach re-applies to the fresh target before any of its scripts run.

### What the spoofer overrides

For every attached target, before resuming it, the spoofer applies:

| Override                                              | Mechanism                                              | Why                                              |
| ----------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------ |
| UA string + full `userAgentMetadata` (platform=Windows) | `Emulation.setUserAgentOverride`                       | Defeats UA Client Hints `getHighEntropyValues()` |
| `navigator.platform` → Windows string                 | `Page.addScriptToEvaluateOnNewDocument` (before scripts) | A page cannot read back a Linux platform         |
| WebGL `UNMASKED_RENDERER_WEBGL` → configured renderer | same injected script                                   | A page cannot sniff a Linux GPU                  |

All four values are configurable (`spoof_ua_template`, `spoof_uach_platform`, `spoof_navigator_platform`, `spoof_webgl_renderer`); none is hardcoded.

### Version tracking

**The spoofed browser version must track the real installed browser.** The feasibility test (F4) found that a spoofed UA whose Chrome major version was _older_ than the real browser (spoofed 128 against a real 150) caused Amazon to treat the client as **unsupported / outdated**.

To avoid that, the spoofer derives the major version at launch from the installed browser via CDP `/json/version` (the "Browser" field) and substitutes it into the `{version}` placeholder in `spoof_ua_template` and the Client Hints metadata. It **never emits a hardcoded version**. `LunaConfig.is_valid()` rejects a `spoof_ua_template` that lacks the `{version}` placeholder for exactly this reason.

If the real version cannot be determined, the spoofer raises a spoofing-configuration error naming the undetermined value and the launch aborts rather than proceeding with a guessed version.

> **Maintenance note.** Because browsers update frequently, there is nothing to pin here on purpose — the version is read from the live browser every launch. If Luna ever tightens the check further, the template and metadata are the place to adjust.

### Verification and failure

After the detail page loads and **before** the play-click, `verify()` evaluates `navigator.userAgentData.getHighEntropyValues(['platform'])` and the launch flow asserts it equals the configured Windows value. A mismatch is a `platform_spoofing_failure`: the launch aborts and the reported platform value is logged. A CDP attach failure, an undeterminable browser version, or an unreadable probe all map to the same failure path and a non-zero launcher exit.

---

## Catalog synchronization

Synchronization retrieves the account's Luna titles and reconciles them against the Luna shortcuts already in the Steam library.

### How the launcher knows it is Luna

Luna titles have **no games.map row** and there is **no `is_luna` dispatcher branch** (both belonged to a superseded earlier draft). The catalog writer gives each title the tags `browser` (`GameTag.BROWSER`) and `luna`, and a `metadata["browser_url"]` composed as:

```
{luna_host}/game/{product_id}
```

where `product_id` is the `amzn1.adg.product.<UUID>` identifier scraped from the browse-view tile. This is written to `library_cache.json`. On launch, `launcher/browser_games.browser_target` reads that record and returns the URL with `kind="stream"` for a `store == "luna"` title. Loading the composed URL makes Luna redirect to its canonical slug-and-ASIN detail page, which carries the Play button; the shared `/player` URL is reached by the play-activation step, not stored per title.

The catalog itself is read by driving the Luna browse view (`{luna_host}/browse?quick_search=my_games`) in the spoofed browser over CDP and scraping the rendered `game_tile` elements. This DOM-scrape strategy (Strategy B) is the default because it uses only what the feasibility test proved works; an authenticated-API strategy (Strategy A) is deferred behind a confirmed endpoint that has not yet been found.

### Subscription and entitlement states

`LunaSubscription` determines the account's entitlement set and maps it to distinct frontend states so the panel renders the right prompt:

| Outcome                           | Meaning                                           | Library entries         |
| --------------------------------- | ------------------------------------------------- | ----------------------- |
| `connected`                       | entitled; titles written / reconciled             | replaced authoritatively |
| `subscription_required`           | a real source reported zero entitlements          | all Luna entries removed |
| `entitlement_error`               | retrieval failed / timed out (distinct)           | left unchanged           |
| `entitlement_source_unavailable`  | no interface could even be attempted (distinct)   | left unchanged           |

Titles granted through Prime inclusion ("In Prime enthalten") are treated as entitled. Malformed tiles (missing the product id) are skipped and their pre-existing entries are preserved. A catalog that returns zero titles removes all Luna entries; a transport or parse failure leaves the existing entries untouched and reports a sync error.

> **Confidence note.** Catalog and entitlement acquisition is the lowest-confidence part of the integration: the feasibility test exercised no Luna catalog or entitlement API. The Prime marker was seen reliably only on the per-title detail page, not on browse tiles, so tile-level entitlement detection is best-effort.

---

## Launching a Luna title

### Launch flow

When you press Play, the launcher resolves the title's browser-game entry, opens the kiosk browser, and runs the Luna-specific sequence (`luna_launch_flow.run_luna_browser_game`):

1. **Spawn** the Luna kiosk via `LunaBrowser` at the detail URL (within 5 seconds of invocation).
2. **Attach** `LunaPlatformSpoofer` browser-level on `browser_game_cdp_port()` **before** navigation.
3. **Verify** the spoofed platform; a mismatch aborts with `platform_spoofing_failure`.
4. **Activate** play via `LunaPlayActivator`: trigger the play action within 15 seconds, then confirm within 30 seconds that the browser reached `/player` and the `<video>` is playing (`readyState === 4 && !paused`). Failure to reach `/player` maps to `launch_failure` and is recorded in the launcher log.
5. **Inject** the reused CDP virtual keyboard over the same CDP session.
6. **Block** on the shared `wait_for_browser_exit(..., log_tag="luna")` until the window closes.

The launcher rejects a launch with a non-zero exit (and logs it) when the `browser_url` is empty or malformed, when its host is not an allowed Luna stream host, when no supported browser is found, or when the SteamOS X11 authority file cannot be located.

### Browser flags for Luna

`LunaBrowser` composes the shared `EdgeBrowser` process layer with Luna-specific flags. The browser command is resolved from the configured `browser_candidates` via the shared `find_cmd()` path, so Luna is not hardcoded to one brand (the feasibility test succeeded on Chrome).

| Flag                                         | Purpose                                                        |
| -------------------------------------------- | -------------------------------------------------------------- |
| `--kiosk` / fullscreen                       | Fullscreen, no window chrome                                   |
| `--user-data-dir=<Luna profile>`             | The dedicated Luna profile (see below)                         |
| `--remote-debugging-port=<browser_game_cdp_port()>` | The CDP port the spoofer, play-activator, and keyboard attach to |
| gamepad flags                                | Web Gamepad feature + button-axis events (from `gamepad_flags`) |
| `--autoplay-policy=no-user-gesture-required` | The stream starts without a click                              |
| `--lang`                                     | Locale                                                         |

### Dedicated Luna profile (no SSO)

**Luna uses its own browser profile, separate from the `edge-auth` profile the other stores use.** There is deliberately **no single sign-on** with xCloud, GOG, Ubisoft, or the download-based Amazon Games integration — Luna requires its own interactive Amazon sign-in in the Luna profile.

The profile defaults to the shared browser-game profile dir (`WEB_GAME_PROFILE_DIR`, `~/.local/share/unifideck/edge-webgames`) only when `luna.profile_dir` is unset; a dedicated dir is recommended so Luna's spoofed Amazon session does not mingle with other browser-game cookies. Because the browser runs as a flatpak, a `--user-data-dir` the sandbox has no permission for is silently not created, so a narrowly scoped `flatpak --user override --filesystem=<Luna profile>` (plus `/run/udev:ro`) is applied first. That override grants the Luna profile dir **only** — never the surrounding plugin state directory, which holds a cleartext-credentials file.

---

## Controller support (Steam Deck)

### Supported target environment

Luna controller input **works in Gaming Mode with a gamepad Steam Input layout on the shortcut** — this is the supported target environment. It is not supported in Desktop Mode.

What makes the Deck controller drive a streamed Luna game, verified on a real Deck (F8):

- The browser flatpak manifest already grants `devices=all`, so no extra device flag is needed.
- A flatpak filesystem override grants read access to `/run/udev` so the Gamepad API can identify the pad (`--filesystem=/run/udev:ro`, applied with the Luna profile override).
- The gamepad browser flags are set before the page loads.
- The Deck controller is exposed by Steam Input as an Xbox-360-style pad via a **gamepad** Steam Input layout on the shortcut.

When a Luna shortcut becomes the active launch target, `LunaStore.request_steam_input_layout` best-effort requests a gamepad Steam Input layout within 5 seconds, reusing `refresh_steam_controller_layout` (the same bounce the xCloud path uses).

### Manual layout selection

If the automatic gamepad layout does not apply within 5 seconds (the request timed out, raised, or returned falsy), the plugin indicates that automatic application did not complete and you set the layout manually, once:

1. In Gaming Mode, open the Luna game's page in your Steam library.
2. Press the **controller / gear** icon to open the controller settings.
3. Choose **Controller Layout** (or **Controller Settings**).
4. Open **Templates** and pick a **Gamepad** template (for example **Gamepad With Joystick Trackpad**).
5. Apply it. The selection persists in Steam Cloud, so you only need to do this once per shortcut.

The key point is simply that the shortcut needs a _gamepad_ layout rather than the keyboard/mouse default; without it, controller input does not reach the streamed game.

---

## Virtual keyboard

The Steam Deck has no physical keyboard, and Steam's overlay keyboard is unavailable because the browser runs outside Steam. The solution is a **virtual keyboard injected via CDP** over the same browser-game CDP port the spoofer and play-activator use (one CDP session for the whole launch).

The injected script wires show/hide to the page's own `focusin`/`focusout` events, so the keyboard appears within 500 ms of a text field gaining focus and hides on blur. Layout selection:

- **AZERTY** for any locale whose language code is `fr`
- **QWERTY** for every other locale, and as the default when the locale cannot be resolved

Because the keyboard and the required spoofer both depend on CDP, a missing CDP dependency aborts the launch and is recorded in the launcher log.

---

## Steam Deck environment

The plugin runs inside PluginLoader, which has no graphical environment, so the launcher injects the missing variables via the shared `clean_env()` before opening the browser:

| Variable                   | Value                           | Reason                               |
| -------------------------- | ------------------------------- | ------------------------------------ |
| `DISPLAY`                  | `:0`                            | X11 session                          |
| `XDG_RUNTIME_DIR`          | `/run/user/1000`                | Bus socket                           |
| `DBUS_SESSION_BUS_ADDRESS` | `unix:path=/run/user/1000/bus`  | D-Bus                                |
| `XAUTHORITY`               | `/run/user/1000/xauth_*` (glob) | X11 auth (randomly named on SteamOS) |
| `GTK_MODULES`              | `""`                            | Suppress canberra warnings           |

SteamOS uses a randomly named xauth file, located by glob. If it cannot be found, the launcher exits non-zero and records the missing-authority-file condition.

---

## Logout and profile handling

Connecting and disconnecting is done once from the plugin (QAM → Luna → Sign in / Logout). Once signed in, the session cookies persist in the Luna profile and are reused on every launch.

On logout, the kiosk is killed first (it owns the cookie database), then `LunaSessionMonitor.clear_cookies()` clears the Amazon cookies in the Luna profile within 5 seconds (using the shared `cookie_writer.py` helper), resulting in a not-connected state. If clearing fails, the previously persisted session state is retained and reported rather than leaving an inconsistent half-cleared profile.

---

## Error handling

Every failure point maps to a distinct state so the panel never gets stuck and always offers a way back to a stable state. The retry/recovery loop retries a network/API operation up to 3 times behind a `recovery_in_progress` indicator, logs each failure with a timestamp, operation name, and description, and restores the last known stable state before exposing a retry action.

The full `Store_Status` value set Luna surfaces: `connected`, `not_connected`, `session_expired`, `session_unknown`, `needs_browser`, `subscription_required`, `entitlement_error`, `entitlement_source_unavailable`, `region_unavailable`, `availability_unknown`, `platform_spoofing_failure`, `launch_failure`, `recovery_in_progress`.

---

## Prerequisites and dependencies

| Prerequisite                     | Verification                                      | Fallback                               |
| -------------------------------- | ------------------------------------------------- | -------------------------------------- |
| **Browser** (flatpak or native)  | `EdgeBrowser.is_installed` / `find_cmd()`         | flatpak install offered (`needs_browser`) |
| **CDP** (websockets / aiohttp)   | required by the spoofer and keyboard              | Launch aborts (spoofer is mandatory)   |
| **Internet connection**          | implicit (Luna web app)                           | errors logged                          |
| **Luna subscription / Prime**    | determined at sync                                | `subscription_required` state          |
| **flatpak** (for installation)   | `shutil.which("flatpak")`                         | `flatpak_not_found` error              |
| **X11 session** (DISPLAY)        | `clean_env()` injects `:0`                        | browser won't open                     |

Unlike the keyboard (which is non-blocking for xCloud), CDP is **not optional** for Luna: the spoofer depends on it, so a missing CDP dependency aborts the launch.

---

## Configuration

All endpoints and parameters are read from `settings.json` under the `stores.luna` prefix (never hardcoded). There are **no OAuth keys** and no games.map launch-routing key.

| Key                        | Usage                                                      |
| -------------------------- | ---------------------------------------------------------- |
| `luna_host`                | Regional Luna host (for example `luna.amazon.de`)          |
| `allowed_stream_hosts`     | Host allow-list for `is_allowed_browser_url` (stream kind) |
| `signin_path`              | Luna sign-in page path                                     |
| `detail_url_pattern`       | Composition of the per-title detail URL                    |
| `player_path`              | The shared `/player` path                                  |
| `catalog_endpoint`         | Catalog endpoint (Strategy A, placeholder until confirmed) |
| `subscription_endpoint`    | Entitlement endpoint (Strategy A, placeholder)             |
| `profile_dir`              | Luna profile dir (defaults to `WEB_GAME_PROFILE_DIR`)      |
| `browser_game_cdp_flavour` | Resolves the browser-game CDP port                         |
| `browser_candidates`       | Browser commands tried in order (Chrome / Edge)            |
| `cloud_browser_flags`      | Kiosk / cloud-gaming browser flags                         |
| `gamepad_flags`            | Gamepad browser flags                                      |
| `flatpak_override_paths`   | Extra filesystem override paths                            |
| `spoof_ua_template`        | UA template with a `{version}` placeholder                 |
| `spoof_uach_platform`      | UA Client Hints platform (Windows)                         |
| `spoof_navigator_platform` | `navigator.platform` override                              |
| `spoof_webgl_renderer`     | WebGL unmasked renderer override                           |

`LunaConfig.is_valid()` names any missing, empty, or malformed key, and specifically flags a `spoof_ua_template` that lacks the `{version}` placeholder — a fixed version would let Luna reject the client as outdated.

---

## Internationalization

- **Catalog / Prime marker.** The browse-view scrape recognizes the German "In Prime enthalten" marker for Prime inclusion. The detail page is reached in the configured Luna host's locale (for example `luna.amazon.de`).
- **Virtual keyboard.** The layout follows the Unifideck locale: `fr-*` → AZERTY, everything else → QWERTY.

---

## Security

- No OAuth tokens exist for Luna; the only persisted secret is the Amazon session cookies in the Luna profile.
- The Luna profile is dedicated and separate from the `edge-auth` profile; there is no SSO with other stores.
- The flatpak filesystem override is scoped to the Luna profile dir (plus `/run/udev:ro`) only, so the plugin state directory — which holds a cleartext-credentials file — is never exposed to the browser sandbox.
- The spoofer runs browser-level and only overrides OS-identity signals; it does not touch Amazon credentials.
- `LD_LIBRARY_PATH` and `LD_PRELOAD` are scrubbed by the launcher to prevent injection via PluginLoader.

---

## Known limitations

1. **Streaming only** — no games are downloaded or installed locally.
2. **The platform spoofer is required** — Luna blocks Linux at game start; without the spoofer every launch fails with an unsupported-OS error.
3. **The spoofed version must track the installed browser** — a spoofed version older than the real browser is rejected as unsupported. The version is derived from the live browser each launch and must not be hardcoded.
4. **No single sign-on** — the Luna profile is separate from the other stores' profile, so Luna needs its own Amazon sign-in.
5. **Controller works in Gaming Mode only** — input requires a gamepad Steam Input layout on the shortcut (see [Controller support](#controller-support-steam-deck)); if auto layout does not complete, set it manually.
6. **Catalog/entitlement confidence** — no Luna catalog or entitlement API is confirmed; the integration scrapes the browse view, and tile-level Prime entitlement is best-effort.
7. **Network quality** — streaming depends on the Internet connection.
