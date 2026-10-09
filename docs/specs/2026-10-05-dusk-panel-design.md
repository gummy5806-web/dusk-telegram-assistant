# Dusk Panel Design

## Goal

Replace the Tkinter capture window with one native macOS panel that reliably opens above the current Space on the display containing the mouse, takes keyboard focus, and reuses a single application process.

## Chosen Approach

Build a small AppKit menu-bar-style application (`LSUIElement`) with one reusable `NSPanel`. BetterTouchTool only sends `dusk://capture`, `dusk://private`, or `dusk://ask`; it no longer starts Python GUI processes. The existing Obsidian `bot/dispatch.py` remains the single business-logic backend.

This is preferred over a BetterTouchTool floating menu because it keeps state, focus, placement, and visual behavior under testable source control. It is preferred over repairing Tkinter because Tkinter cannot reliably model macOS Spaces, accessory full-screen windows, or single-instance URL delivery.

## Components

- `DuskCore`: capture modes, deterministic backend invocation, and display-relative panel placement.
- `DuskPanel`: AppKit lifecycle, URL handling, one `NSPanel`, input and result states.
- `install-dusk-panel.sh`: release build, ad-hoc signing, atomic install into `~/Applications`, Launch Services registration, and app launch.
- `setup-btt-dusk.sh`: idempotent `Caps+N`, `Caps+P`, and `Caps+A` URL triggers.
- yabai rule: Dusk remains unmanaged as a defense in depth measure.

## Interaction

The panel exposes five explicit modes: Smart, Task, Ask, Private, and Link. `Caps+N` opens Smart, `Caps+P` opens Private, and `Caps+A` opens Ask. Tab cycles modes. Enter submits and closes after the result is shown; Command+Enter submits, clears the input, and keeps the panel open.

Smart input is passed through unchanged. Task and Ask add the existing dispatcher prefixes. Private calls the dispatcher's explicit private argument. Link is passed through unchanged and validated by the existing dispatcher.

## Window Behavior

The app has no Dock icon and remains alive after the panel is hidden. Its panel joins all Spaces, is allowed beside full-screen applications, stays above normal windows, and does not disappear when another app deactivates. Each invocation recomputes the target screen from the global mouse position and centers the panel within that screen's visible frame before activating the app and focusing the text field.

Escape hides the panel. Repeated hotkeys reuse the same panel and never create duplicate windows.

## Data Flow

1. BTT opens a Dusk URL.
2. Launch Services starts or addresses the existing Dusk process.
3. The app selects the requested mode, positions and focuses its panel.
4. Submission builds a validated `dispatch.py` process invocation.
5. The backend runs off the main thread with a timeout.
6. The result returns to the main thread and is rendered without blocking the panel.

The app writes no vault files itself. Telegram and the Mac panel therefore continue to share the same dispatcher contract.

## Error Handling

Empty input does not invoke the backend. A missing vault or dispatcher produces a visible error. Process launch failures, non-zero exits, empty output, and timeout are distinct result states. While a request is active, duplicate submission is rejected and controls remain in a deterministic busy state.

## Verification

- Unit tests cover mode-to-argument mapping, input normalization, and monitor selection/frame clamping.
- Swift release build and shell syntax checks must pass.
- Manual integration checks confirm one Dusk process and one window after repeated invocations.
- yabai inspection confirms the panel opens on the mouse display and is unmanaged.
- Visual checks cover a normal Space and a full-screen Space.

## Scope

The first release does not add a model, rewrite dispatch routing, or change vault synchronization. Deep agent-assisted questions remain a future opt-in action after the native capture path is stable.
