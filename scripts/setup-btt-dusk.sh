#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[btt-dusk] %s\n' "$*"
}

warn() {
  printf '[btt-dusk] WARNING: %s\n' "$*" >&2
}

dusk_app="${DUSK_APP_PATH:-$HOME/Applications/Dusk.app}"
btt_app="/Applications/BetterTouchTool.app"
jq_bin="$(command -v jq || true)"

if [[ ! -d "$btt_app" ]]; then
  warn "BetterTouchTool is not installed; skipping Dusk hotkeys."
  exit 0
fi

if [[ ! -x "$dusk_app/Contents/MacOS/DuskPanel" ]]; then
  warn "$dusk_app is missing; skipping Dusk hotkeys."
  exit 0
fi

if [[ -z "$jq_bin" ]]; then
  warn "jq is not installed; skipping Dusk hotkeys."
  exit 0
fi

/usr/bin/open -gj "$btt_app"

triggers=""
for _ in {1..20}; do
  if triggers="$(/usr/bin/osascript -e 'tell application "BetterTouchTool" to get_triggers trigger_type "BTTTriggerTypeKeyboardShortcut"' 2>/dev/null)"; then
    break
  fi
  sleep 0.5
done

if [[ -z "$triggers" ]]; then
  warn "BetterTouchTool scripting API did not become ready."
  exit 1
fi

delete_trigger_uuid() {
  /usr/bin/osascript \
    -e 'on run argv' \
    -e 'tell application "BetterTouchTool" to delete_trigger (item 1 of argv)' \
    -e 'end run' \
    "$1" >/dev/null
}

delete_existing_trigger() {
  local note="$1"
  local uuid

  while IFS= read -r uuid; do
    [[ -n "$uuid" ]] || continue
    delete_trigger_uuid "$uuid"
  done < <(printf '%s' "$triggers" | "$jq_bin" -r --arg note "$note" '.[] | select(.BTTGestureNotes? == $note) | .BTTUUID')
}

add_trigger() {
  local key="$1"
  local keycode="$2"
  local note="$3"
  local route="$4"
  local command
  local payload

  printf -v command '/usr/bin/open -b com.asan.dusk-panel %q' "dusk://$route"

  payload="$("$jq_bin" -nc \
    --arg key "$key" \
    --argjson keycode "$keycode" \
    --arg note "$note" \
    --arg command "$command" \
    '{
      BTTTriggerBelongsToPreset: "Default",
      BTTTriggerType: 0,
      BTTTriggerClass: "BTTTriggerTypeKeyboardShortcut",
      BTTAdditionalConfiguration: "1966080",
      BTTShortcutKeyboardType: 0,
      BTTTriggerOnDown: 1,
      BTTLayoutIndependentChar: $key,
      BTTShortcutKeyCode: $keycode,
      BTTShortcutAdvancedModifierKeys: "1966080",
      BTTShortcutModifierKeys: 1966080,
      BTTOrder: 0,
      BTTAutoAdaptToKeyboardLayout: 0,
      BTTGestureNotes: $note,
      BTTBelongsToApp: "Global",
      BTTActionsToExecute: [{
        BTTIsPureAction: true,
        BTTPredefinedActionType: 137,
        BTTPredefinedActionName: "Execute Terminal Command (Async, non-blocking)",
        BTTTerminalCommand: $command,
        BTTOrder: 0
      }]
    }')"

  /usr/bin/osascript \
    -e 'on run argv' \
    -e 'tell application "BetterTouchTool" to add_new_trigger (item 1 of argv)' \
    -e 'end run' \
    "$payload"
}

normal_note="Dusk capture - Caps+N"
private_note="Dusk private capture - Caps+P"
ask_note="Dusk ask - Caps+A"

new_uuids=()
replacement_ready=false
rollback_new_triggers() {
  [[ "$replacement_ready" == false ]] || return
  local uuid
  for uuid in "${new_uuids[@]}"; do
    delete_trigger_uuid "$uuid" 2>/dev/null || true
  done
}
trap rollback_new_triggers EXIT

normal_uuid="$(add_trigger n 45 "$normal_note" capture)"
[[ -n "$normal_uuid" ]] || exit 1
new_uuids+=("$normal_uuid")
private_uuid="$(add_trigger p 35 "$private_note" private)"
[[ -n "$private_uuid" ]] || exit 1
new_uuids+=("$private_uuid")
ask_uuid="$(add_trigger a 0 "$ask_note" ask)"
[[ -n "$ask_uuid" ]] || exit 1
new_uuids+=("$ask_uuid")
replacement_ready=true

delete_existing_trigger "$normal_note"
delete_existing_trigger "$private_note"
delete_existing_trigger "$ask_note"
trap - EXIT

log "Caps+N installed: $normal_uuid"
log "Caps+P installed: $private_uuid"
log "Caps+A installed: $ask_uuid"
