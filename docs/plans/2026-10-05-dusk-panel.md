# Dusk Panel Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use test-driven-development to implement this plan task-by-task.

**Goal:** Build and install a native single-instance Dusk capture panel with reliable focus, full-screen support, mouse-display placement, and BTT hotkeys.

**Architecture:** A Swift package separates pure routing and geometry logic into `DuskCore` from the AppKit executable. A shell installer builds and atomically installs a signed application bundle, then existing restore scripts register URL-based BTT triggers.

**Tech Stack:** Swift 6.2, AppKit, XCTest, Swift Package Manager, Bash, BetterTouchTool AppleScript API, yabai.

---

### Task 1: Create the tested core contract

**Files:**
- Create: `config/dusk-panel/Package.swift`
- Create: `config/dusk-panel/Tests/DuskCoreTests/CaptureRequestTests.swift`
- Create: `config/dusk-panel/Tests/DuskCoreTests/PanelPlacementTests.swift`
- Create: `config/dusk-panel/Sources/DuskCore/CaptureRequest.swift`
- Create: `config/dusk-panel/Sources/DuskCore/PanelPlacement.swift`

**Steps:**
1. Write tests for URL mode parsing, dispatcher arguments, empty-input validation, target-screen selection, and frame clamping.
2. Run `swift run --package-path config/dusk-panel DuskCoreTests` and verify the test runner fails because `DuskCore` is absent.
3. Implement the minimal pure types.
4. Run `swift run --package-path config/dusk-panel DuskCoreTests` and verify the tests pass.

### Task 2: Build the native panel

**Files:**
- Create: `config/dusk-panel/Sources/DuskPanel/main.swift`
- Create: `config/dusk-panel/Sources/DuskPanel/AppDelegate.swift`
- Create: `config/dusk-panel/Sources/DuskPanel/PanelController.swift`
- Create: `config/dusk-panel/Sources/DuskPanel/DispatchRunner.swift`
- Create: `config/dusk-panel/Resources/Info.plist`

**Steps:**
1. Add a lifecycle smoke test for command construction used by the runner.
2. Implement the accessory application, URL handling, reusable full-screen-capable panel, keyboard behavior, and asynchronous dispatcher process.
3. Run the standalone test runner and `swift build -c release`.

### Task 3: Install and wire restore behavior

**Files:**
- Create: `scripts/install-dusk-panel.sh`
- Create: `scripts/build-dusk-panel.sh`
- Create: `scripts/setup-dusk-vault.sh`
- Modify: `scripts/setup-btt-dusk.sh`
- Modify: `install.sh`
- Modify: `config/skhdrc`
- Modify: `config/yabairc`
- Modify: `scripts/doctor.sh`
- Modify: `scripts/snapshot-current.sh`

**Steps:**
1. Build a universal prebuilt executable, then create an app bundle in a temporary directory, ad-hoc sign it, atomically replace `~/Applications/Dusk.app`, and register it with Launch Services.
2. Restore the private `dusk-vault` backend when it is absent.
3. Replace Python BTT commands with URL opens and add `Caps+A`.
4. Add fallback shortcuts and the unmanaged yabai rule.
5. Integrate installation and diagnostics into the restore flow.
6. Run shell syntax checks and the installer.

### Task 4: Verify the installed behavior

**Files:**
- Modify: `README.md`
- Modify: `docs/yabai-setup.md`

**Steps:**
1. Invoke every URL and inspect process/window counts.
2. Move the pointer across displays and verify the panel follows it.
3. Verify focus, Escape, repeated invocation, and a full-screen Space.
4. Update recovery and keymap documentation with exact behavior.
5. Run all tests and inspect the final scoped diff.
