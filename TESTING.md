# Testing guide for BetterFlow Sync agent

This guide covers how to manually test the BetterFlow Sync desktop agent after installing from the DMG.

## Prerequisites

- macOS 10.15 or later
- A BetterFlow account (https://betterflow.eu)
- Internet connection for initial login

ActivityWatch is **bundled** inside the app - you do not need to install it separately.

## Installation

1. Open `BetterFlow Sync.dmg`
2. Drag **BetterFlow Sync** into the **Applications** folder
3. Launch from Applications (or Spotlight: "BetterFlow Sync")
4. On first launch, macOS may show "app from unidentified developer" - go to **System Settings > Privacy & Security** and click **Open Anyway**

## First launch (setup wizard)

On first run the setup wizard appears:

| Step | What to verify |
|------|---------------|
| Welcome screen | Displays BetterFlow branding, "Get Started" button works |
| Login | Clicking "Sign In" opens your browser to the BetterFlow authorize page |
| Browser callback | After signing in, the browser shows "Authorization Successful" and the app updates to show your email |
| Completion | Wizard closes, tray icon appears in menu bar |

**Pass criteria**: Tray icon turns **green** within 30 seconds of login completing.

## Test cases

### 1. Tray icon states

| Action | Expected tray color | Expected menu text |
|--------|--------------------|--------------------|
| Normal operation | Green | "Active" |
| Click "Pause Tracking" | Gray | "Paused" |
| Click "Resume Tracking" | Green | "Active" |
| Disconnect Wi-Fi | Yellow | "Offline" |
| Reconnect Wi-Fi | Green (within 60s) | "Active" |
| Click "Private Time" | Dark gray | "Private Time" |
| Click "End Private Time" | Green | "Active" |

### 2. Hours tracking

1. Let the app run for 5+ minutes while actively using your computer
2. Click the tray icon
3. **Verify**: "Hours today" shows a non-zero value (e.g. "0h 5m")
4. **Verify**: The hours value matches approximately how long you have been active
5. Open https://app.betterflow.eu/dashboard and verify hours appear there too

### 3. Project selection

1. Click the tray icon
2. Under "Recent Projects", select a project
3. **Verify**: The project name appears under "Running Project"
4. **Verify**: "Stop (Xh Ym)" button appears next to the project name
5. Click "Stop" - project clears back to "No project selected"

### 4. Pause and resume

1. Click tray > Quick Menu > "Pause Tracking"
2. **Verify**: Tray turns gray, menu shows "Paused"
3. Wait 2 minutes - hours should NOT increase
4. Click tray > Quick Menu > "Resume Tracking"
5. **Verify**: Tray turns green, hours resume incrementing

### 5. Sleep/wake behavior

1. Pause tracking manually (tray > Pause Tracking)
2. Close your laptop lid (sleep), then open it (wake)
3. **Verify**: App stays paused (gray icon) - it should NOT auto-resume
4. Resume tracking manually
5. Close lid again, then open
6. **Verify**: App resumes automatically (green icon)

### 6. Screen lock/unlock

1. Lock your screen (Ctrl+Cmd+Q)
2. **Verify** (after unlock): If you were paused before locking, you stay paused
3. If you were active before locking, tracking resumes after unlock

### 7. Notifications

1. Quit ActivityWatch if it is running externally (the bundled version handles it)
2. On first launch, if no Accessibility permission is granted to the window tracker:
   - **Verify**: A macOS notification appears asking to grant Accessibility permission
3. If an update is available:
   - **Verify**: A notification appears with the new version number

### 8. Private time reminders

1. Click tray > "Private Time" to start private mode
2. Set the reminder interval to 15 minutes (tray > Private Time Reminder > Every 15 Minutes)
3. Wait 15 minutes
4. **Verify**: A notification reminds you that private time is still active
5. Click "End Private Time"
6. **Verify**: Reminders stop

### 9. Break time reminders

1. Set break reminder to 1 hour (tray > Break Time Reminder > Every 1 Hour)
2. Work for 1 hour
3. **Verify**: A notification reminds you to take a break
4. Pause tracking - break timer should reset
5. Resume tracking - timer starts from zero again

### 10. Logout and re-login

1. Click tray > "Log Out"
2. **Verify**: Tray turns amber, shows "Waiting for browser login..."
3. **Verify**: Browser opens to the authorize page
4. Sign in again
5. **Verify**: Tray turns green, hours and projects reload
6. **Verify**: Previous hours are preserved (the logout did not reset the day's data)

### 11. Offline queue

1. Disconnect Wi-Fi
2. Continue working for 2-3 minutes
3. Click tray icon
4. **Verify**: Tray is yellow, Diagnostics > Queue shows a non-zero event count
5. Reconnect Wi-Fi
6. **Verify**: Within 60 seconds, tray turns green and queue drains to 0

### 12. Sync interval

1. Tray > Preferences > Sync Interval > 30s
2. **Verify**: The "Last sync" time in Diagnostics updates every ~30 seconds
3. Change back to 60s
4. **Verify**: Sync interval returns to normal

### 13. Export logs

1. Tray > Preferences > Export Logs
2. **Verify**: A zip file appears on your Desktop named `betterflow-logs-YYYYMMDD-HHMMSS.zip`
3. **Verify**: Finder opens showing the file
4. Unzip it - should contain `logs/` folder and `config-redacted.json`
5. **Verify**: `config-redacted.json` does NOT contain `device_id` or tokens

### 14. Preferences persistence

1. Change several preferences:
   - Sync interval to 120s
   - Disable "Hash Window Titles"
   - Enable "Debug Mode"
2. Quit the app (tray > Quit)
3. Relaunch
4. **Verify**: All preferences are preserved

### 15. Dashboard and project manager links

1. Tray > "Show My Dashboard"
2. **Verify**: Browser opens to your BetterFlow dashboard
3. Tray > "Project Manager - Start / Stop / Create"
4. **Verify**: Browser opens to the projects page

## Regression checks (bug fixes in this build)

These specifically verify the fixes in this release:

| # | What was fixed | How to verify |
|---|---------------|---------------|
| 1 | Notifications were silently failing | Grant Accessibility permission prompt should appear as a macOS notification (not just a log entry) |
| 2 | Scheduler died after logout/re-login | Log out, log back in, verify sync continues (tray updates "Last sync" time) |
| 3 | Wake/unlock ignored user pause | Pause manually, sleep/wake laptop, verify it stays paused |
| 4 | Tray "Hours today" was not updating | After 5 min of work, tray menu should show accurate hours |
| 5 | Pre-release version check crashed | If on a beta channel, update check should not error (check logs for "Update check failed") |

## Log file locations

If something goes wrong, check the logs:

```
~/Library/Logs/BetterFlow Sync/betterflow-sync.log
```

Enable Debug Mode (tray > Preferences > Debug Mode) for verbose logging.

## Reporting bugs

1. Reproduce the issue
2. Export logs (tray > Preferences > Export Logs)
3. Note the steps to reproduce, expected vs actual behavior
4. Send the log zip + reproduction steps to the dev team
