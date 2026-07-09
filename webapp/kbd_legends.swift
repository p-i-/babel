// kbd_legends — one-shot: print the user's ACTIVE macOS keyboard layout as JSON.
//
//   { "source": "British", "legends": { "KeyQ": "q", "KeyZ": "z", ... } }
//
// "legends" maps browser event.code positions -> the character the user's OS
// layout produces there (unshifted). This is the truth about their PHYSICAL
// keycaps (incl. ANSI/ISO edge keys), used to label the on-screen target-language
// keyboard so "find the z key" means something. Deliberately stdout-only, run at
// every server boot: the active input source is MUTABLE state — a cached file
// would eventually describe yesterday's keyboard (see exp-09/exp-08 morals:
// instruments must not lie).
//
// Build: swiftc -O -o kbd_legends kbd_legends.swift   (server auto-builds)

import Carbon
import Foundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(("kbd_legends: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

// macOS virtual keycode -> browser event.code (positional, both stable)
let vkToCode: [(Int, String)] = [
    (0, "KeyA"), (1, "KeyS"), (2, "KeyD"), (3, "KeyF"), (4, "KeyH"), (5, "KeyG"),
    (6, "KeyZ"), (7, "KeyX"), (8, "KeyC"), (9, "KeyV"), (10, "IntlBackslash"),
    (11, "KeyB"), (12, "KeyQ"), (13, "KeyW"), (14, "KeyE"), (15, "KeyR"),
    (16, "KeyY"), (17, "KeyT"), (18, "Digit1"), (19, "Digit2"), (20, "Digit3"),
    (21, "Digit4"), (22, "Digit6"), (23, "Digit5"), (24, "Equal"), (25, "Digit9"),
    (26, "Digit7"), (27, "Minus"), (28, "Digit8"), (29, "Digit0"),
    (30, "BracketRight"), (31, "KeyO"), (32, "KeyU"), (33, "BracketLeft"),
    (34, "KeyI"), (35, "KeyP"), (37, "KeyL"), (38, "KeyJ"), (39, "Quote"),
    (40, "KeyK"), (41, "Semicolon"), (42, "Backslash"), (43, "Comma"),
    (44, "Slash"), (45, "KeyN"), (46, "KeyM"), (47, "Period"), (50, "Backquote"),
]

guard let srcUnmanaged = TISCopyCurrentKeyboardLayoutInputSource() else {
    fail("no current keyboard layout input source")
}
let src = srcUnmanaged.takeRetainedValue()

var name = "?"
if let p = TISGetInputSourceProperty(src, kTISPropertyLocalizedName) {
    name = Unmanaged<CFString>.fromOpaque(p).takeUnretainedValue() as String
}
guard let dataP = TISGetInputSourceProperty(src, kTISPropertyUnicodeKeyLayoutData) else {
    fail("layout \(name) has no uchr data (CJK/IME input source? switch to a plain layout)")
}
let data = Unmanaged<CFData>.fromOpaque(dataP).takeUnretainedValue() as Data

// Physical form factor of the ATTACHED hardware (ANSI/ISO/JIS) — drives the
// on-screen grid geometry (ISO has an extra key left of Z; Backslash sits at
// the home-row end instead of above Enter).
let layoutKind = Int(KBGetLayoutType(Int16(LMGetKbdType())))
let physical: String
switch layoutKind {
case kKeyboardISO: physical = "iso"
case kKeyboardJIS: physical = "jis"
case kKeyboardANSI: physical = "ansi"
default: physical = "unknown"
}

var legends: [String: String] = [:]
data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
    let layout = raw.bindMemory(to: UCKeyboardLayout.self).baseAddress!
    for (vk, code) in vkToCode {
        var deadKeys: UInt32 = 0
        var chars = [UniChar](repeating: 0, count: 8)
        var length = 0
        let err = UCKeyTranslate(layout, UInt16(vk), UInt16(kUCKeyActionDisplay),
                                 0,                       // no modifiers
                                 UInt32(LMGetKbdType()),
                                 OptionBits(kUCKeyTranslateNoDeadKeysBit),
                                 &deadKeys, chars.count, &length, &chars)
        guard err == noErr, length > 0 else { continue }
        let s = String(utf16CodeUnits: chars, count: length)
        if !s.isEmpty, s.rangeOfCharacter(from: .controlCharacters) == nil,
           s != " " {
            legends[code] = s
        }
    }
}

let out: [String: Any] = ["source": name, "physical": physical, "legends": legends]
let json = try! JSONSerialization.data(withJSONObject: out,
                                       options: [.sortedKeys])
FileHandle.standardOutput.write(json)
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
