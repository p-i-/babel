// wincap — capture the FOCUSED window's own bitmap to a PNG.
//
// Why not `screencapture`: `-m` grabs the whole main DISPLAY (desktop, Discord,
// whatever is frontmost — field 2026-07-08 the tutor's "eyes" kept seeing Discord),
// and `-l <id>` grabs the on-screen COMPOSITED representation, which Stage Manager
// tilts into a 3D thumbnail. CGWindowListCreateImage(.optionIncludingWindow) reads
// the window's OWN backing store: flat, rectangular, full content — occlusion-proof
// and Stage-Manager-proof. It's the focused window because the frontmost normal
// (layer-0) app window is the active app's key window = what has input focus.
//
// Usage: wincap <out.png>   → exit 0 and prints "owner\ttitle"; nonzero on failure.
import CoreGraphics
import Foundation
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "wincap.png"
// system-owned overlays that are never "the student's window"
let skip: Set<String> = ["WindowManager", "Dock", "Window Server", "Control Center",
                         "Notification Center", "Spotlight", "SystemUIServer", "Wallpaper"]

func die(_ msg: String, _ code: Int32) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

guard let arr = CGWindowListCopyWindowInfo(
        [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]]
else { die("no window list (Screen-Recording permission?)", 1) }

// front-to-back z-order; take the first real app window of a sane size
var pick: (id: Int, owner: String, title: String)? = nil
for w in arr {
    guard (w[kCGWindowLayer as String] as? Int ?? -1) == 0 else { continue }
    let owner = w[kCGWindowOwnerName as String] as? String ?? "?"
    guard !skip.contains(owner) else { continue }
    guard let b = w[kCGWindowBounds as String] as? [String: Any],
          let ww = b["Width"] as? Double, let hh = b["Height"] as? Double,
          ww > 300, hh > 200 else { continue }
    pick = (w[kCGWindowNumber as String] as? Int ?? -1, owner,
            w[kCGWindowName as String] as? String ?? "")
    break
}
guard let win = pick else { die("no focusable app window found", 2) }

guard let img = CGWindowListCreateImage(
        .null, .optionIncludingWindow, CGWindowID(win.id),
        [.boundsIgnoreFraming, .nominalResolution])
else { die("capture failed for window \(win.id) (Screen-Recording permission?)", 3) }

let rep = NSBitmapImageRep(cgImage: img)
guard let data = rep.representation(using: .png, properties: [:]) else {
    die("png encode failed", 4)
}
do { try data.write(to: URL(fileURLWithPath: out)) }
catch { die("write failed: \(error)", 5) }
print("\(win.owner)\t\(win.title)")
