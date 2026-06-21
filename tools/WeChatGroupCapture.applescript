on run argv
  try
    set groupName to "🈲言-2六便士AI吟诗"
    tell application "WeChat" to activate
    delay 1.0
    tell application "System Events"
      tell process "WeChat"
        set frontmost to true
        set theWindow to front window
        set {wx, wy} to position of theWindow
        set {ww, wh} to size of theWindow
      end tell

      set the clipboard to groupName
      click at {wx + 90, wy + 22}
      delay 0.2
      keystroke "a" using command down
      delay 0.1
      keystroke "v" using command down
      delay 0.8
      key code 36
      delay 1.0
      key code 53
      delay 0.3

      tell process "WeChat"
        set theWindow to front window
        set {wx, wy} to position of theWindow
        set {ww, wh} to size of theWindow
      end tell
      click at {wx + (ww div 2), wy + (wh div 2)}
      delay 0.2
      keystroke "a" using command down
      delay 0.2
      keystroke "c" using command down
      delay 0.8
    end tell
  on error errMsg number errNum
    set the clipboard to "__CODEX_WECHAT_CAPTURE_ERROR__ " & errNum & " " & errMsg
    error errMsg number errNum
  end try
end run
