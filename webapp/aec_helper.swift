// aec_helper — macOS system echo cancellation (Voice-Processing I/O) as a Unix filter.
//
// The OS-level AEC that FaceTime/Siri use, packaged so any process can rent it:
//   stdin  : framed protocol (see below) carrying 24 kHz mono int16 PCM to PLAY
//   stdout : raw 16 kHz mono int16 PCM — the echo-cancelled MICROPHONE
//   stderr : one "ready" line at boot + any faults (never on the audio path)
//
// AEC needs capture and render inside the SAME audio unit (it subtracts what it
// knows it played), so this helper owns both directions: playback you pipe in is
// the far-end reference; what comes back on stdout has that reference subtracted.
// Sound from OTHER processes is NOT cancelled — by design (it is "the room").
//
// stdin frames: 1 byte type + 4 bytes little-endian payload length + payload
//   'A' : payload = 24 kHz mono int16 PCM — append to the playback queue
//   'F' : payload empty — FLUSH: drop all queued playback now (barge-in)
//   'P' : payload empty — PAUSE: stop the audio unit entirely (mic AND playback).
//         Releases macOS's voice-processing grip on the device: while a VPIO
//         session is live, every OTHER app's mic capture is ~30 dB attenuated
//         (field 2026-07-04 — broke the user's dictation app); pausing frees it.
//   'R' : payload empty — RESUME: start the audio unit again.
// EOF on stdin (or the reader dying) = clean shutdown.
//
// Flags: --no-aec  (identical plumbing, voice processing BYPASSED — the A/B control)
//        --meter   (log mic peaks to stderr, debug only)
//
// Implementation note: we drive kAudioUnitSubType_VoiceProcessingIO through the
// C AudioUnit API directly rather than AVAudioEngine's setVoiceProcessingEnabled.
// The raw unit gives explicit client formats, device pinning, and AGC/ducking/
// bypass control the wrapper doesn't expose. (The wrapper also has quirks on
// macOS 14.6 — a 7-ch ghost format on a 1-ch mic, -10865 on any explicit tap
// format — but was never PROVEN broken; see README finding ① before blaming it.)
//
// Build: ./build.sh   (swiftc -O; no Xcode project needed)
// Probe: ./probe_echo.py — measures the attenuation this actually delivers.

import AudioToolbox
import Foundation

let noAEC = CommandLine.arguments.contains("--no-aec")

let MIC_RATE = 16000.0      // stdout — matches the Gemini Live API input format
let PLAY_RATE = 24000.0     // stdin  — matches the Live API output format
let CLIENT_RATE = 24000.0   // VPIO client rate, BOTH sides — the unit initializes only
                            // with symmetric client rates (16k/24k fails: -10875), so
                            // the mic side is converted 24k -> 16k before stdout.

func elog(_ s: String) {
    FileHandle.standardError.write(("aec_helper: " + s + "\n").data(using: .utf8)!)
}

signal(SIGPIPE, SIG_IGN)   // stdout reader gone -> write fails -> we exit cleanly

// ── byte FIFOs (audio-thread friendly: no allocation while popping) ──────────
final class ByteFIFO {
    private var buf = Data()
    private var head = 0
    private let lock = NSLock()

    func push(_ d: Data) {
        lock.lock(); buf.append(d); lock.unlock()
    }

    /// Copy up to `n` bytes into `dst`; returns bytes copied.
    func pop(into dst: UnsafeMutableRawPointer, max n: Int) -> Int {
        lock.lock(); defer { lock.unlock() }
        let k = min(n, buf.count - head)
        if k > 0 {
            buf.withUnsafeBytes { raw in
                _ = memcpy(dst, raw.baseAddress!.advanced(by: head), k)
            }
            head += k
            if head > 1 << 20 {                 // compact occasionally
                buf.removeFirst(head); head = 0
            }
        }
        return k
    }

    func popAll() -> Data {
        lock.lock(); defer { lock.unlock() }
        let d = head > 0 ? buf.subdata(in: head..<buf.count) : buf
        buf = Data(); head = 0
        return d
    }

    func clear() {
        lock.lock(); buf = Data(); head = 0; lock.unlock()
    }
}

// True globals (namespaced) so the C-convention callbacks below capture nothing —
// in a top-level-code file, bare `var`s would make them "local functions".
enum G {
    static let playFIFO = ByteFIFO()  // stdin -> render callback (reference + speakers)
    static let micFIFO = ByteFIFO()   // input callback -> stdout writer thread
    static let micSem = DispatchSemaphore(value: 0)
    static var au: AudioUnit!
    static var meter = false
    static var micScratch = [UInt8](repeating: 0, count: 32768)
    static var tapCount = 0
}
G.meter = CommandLine.arguments.contains("--meter")

// ── the Voice-Processing I/O unit ────────────────────────────────────────────
var desc = AudioComponentDescription(
    componentType: kAudioUnitType_Output,
    componentSubType: kAudioUnitSubType_VoiceProcessingIO,
    componentManufacturer: kAudioUnitManufacturer_Apple,
    componentFlags: 0, componentFlagsMask: 0)
guard let comp = AudioComponentFindNext(nil, &desc) else {
    elog("FATAL: VoiceProcessingIO component not found"); exit(2)
}
var unitOpt: AudioUnit?
var st = AudioComponentInstanceNew(comp, &unitOpt)
guard st == noErr, let au = unitOpt else {
    elog("FATAL: AudioComponentInstanceNew: \(st)"); exit(2)
}
G.au = au

func setProp<T>(_ pid: AudioUnitPropertyID, _ scope: AudioUnitScope,
                _ element: AudioUnitElement, _ value: inout T, _ what: String,
                fatal: Bool = true) {
    let st = withUnsafePointer(to: &value) { p in
        AudioUnitSetProperty(G.au, pid, scope, element, p, UInt32(MemoryLayout<T>.size))
    }
    if st != noErr {
        elog("\(fatal ? "FATAL" : "note"): set \(what): \(st)")
        if fatal { exit(2) }
    }
}

// I/O on: element 1 = mic side, element 0 = speaker side
var one: UInt32 = 1
setProp(kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &one, "EnableIO(mic)")
setProp(kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &one, "EnableIO(speaker)")

// Pin the REAL default devices onto the unit. Left to its own devices (ha), VPIO
// builds an internal aggregate that chokes on virtual devices (BlackHole lives on
// this machine) — the silent-mic failure mode.
func defaultDevice(_ selector: AudioObjectPropertySelector, _ what: String) -> AudioDeviceID {
    var addr = AudioObjectPropertyAddress(mSelector: selector,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var dev = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                        &addr, 0, nil, &size, &dev)
    if st != noErr { elog("note: get \(what): \(st)") }
    return dev
}
func deviceNamed(_ substr: String) -> AudioDeviceID? {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(0)
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),
                                         &addr, 0, nil, &size) == noErr else { return nil }
    var devs = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &addr, 0, nil, &size, &devs) == noErr else { return nil }
    for dev in devs {
        var nameAddr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName,
                                                  mScope: kAudioObjectPropertyScopeGlobal,
                                                  mElement: kAudioObjectPropertyElementMain)
        var name: Unmanaged<CFString>?
        var nsize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        if AudioObjectGetPropertyData(dev, &nameAddr, 0, nil, &nsize, &name) == noErr,
           let n = name?.takeRetainedValue() as String?, n.contains(substr) {
            return dev
        }
    }
    return nil
}

if !CommandLine.arguments.contains("--no-pin") {
    var inDev = defaultDevice(kAudioHardwarePropertyDefaultInputDevice, "default input")
    var outDev = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice, "default output")
    // --out-name SUBSTR: render to a named device instead (probe trick: pointing
    // the render at BlackHole takes the real speakers OUT of the AEC reference,
    // so a speaker-played clip can stand in for a human voice in pass-through tests)
    if let i = CommandLine.arguments.firstIndex(of: "--out-name"),
       i + 1 < CommandLine.arguments.count {
        if let dev = deviceNamed(CommandLine.arguments[i + 1]) {
            outDev = dev
        } else {
            elog("FATAL: no output device matching \(CommandLine.arguments[i + 1])")
            exit(2)
        }
    }
    elog("pinning devices: in=\(inDev) out=\(outDev)")
    setProp(kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 1, &inDev,
            "CurrentDevice(mic)", fatal: false)
    setProp(kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &outDev,
            "CurrentDevice(speaker)", fatal: false)
}
if !CommandLine.arguments.contains("--agc") {
    // AGC OFF BY DEFAULT (field 2026-07-04): VPIO's AGC adjusts the HARDWARE
    // input volume — a device-GLOBAL setting — and walked it down to 15/100
    // during lessons, silently crippling every other mic app on the machine
    // (Shoum recorded ~15dB under its no-speech gate). macOS restores the gain
    // on clean teardown, but concurrent use was broken. --agc re-enables for
    // experiments.
    var zero: UInt32 = 0
    setProp(kAUVoiceIOProperty_VoiceProcessingEnableAGC, kAudioUnitScope_Global, 0,
            &zero, "AGC off (default; --agc to enable)", fatal: false)
}

// Client stream formats — the unit's converters bridge to the hardware rates.
func monoInt16(_ rate: Float64) -> AudioStreamBasicDescription {
    AudioStreamBasicDescription(
        mSampleRate: rate, mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
        mBytesPerPacket: 2, mFramesPerPacket: 1, mBytesPerFrame: 2,
        mChannelsPerFrame: 1, mBitsPerChannel: 16, mReserved: 0)
}
// --try-rates MIC,PLAY : set those client rates, attempt initialize, report, exit.
// (Diagnostic for the rate-support matrix; see README finding ③.)
var tryRates: (Double, Double)? = nil
if let i = CommandLine.arguments.firstIndex(of: "--try-rates"),
   i + 1 < CommandLine.arguments.count {
    let parts = CommandLine.arguments[i + 1].split(separator: ",").compactMap { Double($0) }
    if parts.count == 2 { tryRates = (parts[0], parts[1]) }
}

var micFmt = monoInt16(tryRates?.0 ?? CLIENT_RATE)
var playFmt = monoInt16(tryRates?.1 ?? PLAY_RATE)
setProp(kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &micFmt,
        "mic client format 24k int16")
setProp(kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &playFmt,
        "play client format 24k int16")

if let (m, p) = tryRates {
    let st = AudioUnitInitialize(au)
    elog("try-rates mic=\(Int(m)) play=\(Int(p)) -> \(st == noErr ? "OK" : "FAIL \(st)")")
    exit(st == noErr ? 0 : 1)
}

if noAEC {
    var bypass: UInt32 = 1
    setProp(kAUVoiceIOProperty_BypassVoiceProcessing, kAudioUnitScope_Global, 0,
            &bypass, "BypassVoiceProcessing")
}
if #available(macOS 14.0, *) {
    // Default config DUCKS other apps' audio while the mic is live. Keep the helper
    // honest: cancel only our own playback, leave the room alone. Set in BYPASS mode
    // too — field ears caught the default ducking crushing a clip 15 dB in a
    // --no-aec control pass, which silently skewed the A/B comparison.
    var duck = AUVoiceIOOtherAudioDuckingConfiguration(
        mEnableAdvancedDucking: false, mDuckingLevel: .min)
    setProp(kAUVoiceIOProperty_OtherAudioDuckingConfiguration, kAudioUnitScope_Global,
            0, &duck, "OtherAudioDuckingConfiguration", fatal: false)
}

// ── callbacks (C conventions — capture-free, state via G) ────────────────────
let inputCallback: AURenderCallback = { _, flags, ts, bus, nFrames, _ in
    let nbytes = Int(nFrames) * 2
    guard nbytes <= G.micScratch.count else { return noErr }
    return G.micScratch.withUnsafeMutableBytes { raw in
        var abl = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(mNumberChannels: 1, mDataByteSize: UInt32(nbytes),
                                  mData: raw.baseAddress))
        let st = AudioUnitRender(G.au, flags, ts, bus, nFrames, &abl)
        if st != noErr { return st }
        let got = Int(abl.mBuffers.mDataByteSize)
        if G.meter {
            G.tapCount += 1
            if G.tapCount % 100 == 1 {
                let samples = raw.bindMemory(to: Int16.self)
                var peak: Int16 = 0
                for i in 0..<got / 2 { peak = max(peak, Int16(abs(Int32(samples[i])))) }
                elog("mic cb #\(G.tapCount) frames=\(nFrames) peak=\(peak)")
            }
        }
        G.micFIFO.push(Data(bytes: raw.baseAddress!, count: got))
        G.micSem.signal()
        return noErr
    }
}

let renderCallback: AURenderCallback = { _, _, _, _, _, io in
    guard let io = io else { return noErr }
    let buf = UnsafeMutableAudioBufferListPointer(io)[0]
    guard let dst = buf.mData else { return noErr }
    let want = Int(buf.mDataByteSize)
    let got = G.playFIFO.pop(into: dst, max: want)
    if got < want {                              // ran dry: pad with silence
        memset(dst.advanced(by: got), 0, want - got)
    }
    return noErr
}

var inCb = AURenderCallbackStruct(inputProc: inputCallback, inputProcRefCon: nil)
setProp(kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global, 1, &inCb,
        "input callback")
var outCb = AURenderCallbackStruct(inputProc: renderCallback, inputProcRefCon: nil)
setProp(kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &outCb,
        "render callback")

st = AudioUnitInitialize(au)
if st != noErr { elog("FATAL: AudioUnitInitialize: \(st)"); exit(2) }
st = AudioOutputUnitStart(au)
if st != noErr { elog("FATAL: AudioOutputUnitStart: \(st) (mic permission?)"); exit(2) }

// ── stdout writer thread: 24k -> 16k SRC, then write ─────────────────────────
// (Keeps both the converter and any pipe stalls off the audio threads.)
let stdoutFH = FileHandle.standardOutput

final class SRCState {
    var pending = Data()        // unconsumed 24k input
    var scratch = [UInt8](repeating: 0, count: 1 << 16)   // stable input window
}
let srcState = SRCState()
let srcPause: OSStatus = -900_001   // custom: "no more input right now"

var in24 = monoInt16(CLIENT_RATE)
var out16 = monoInt16(MIC_RATE)
var srcConvOpt: AudioConverterRef?
st = AudioConverterNew(&in24, &out16, &srcConvOpt)
guard st == noErr, let srcConv = srcConvOpt else {
    elog("FATAL: AudioConverterNew 24k->16k: \(st)"); exit(2)
}

let srcInputProc: AudioConverterComplexInputDataProc = { _, ioNumPackets, ioData, _, refCon in
    let s = Unmanaged<SRCState>.fromOpaque(refCon!).takeUnretainedValue()
    let availFrames = s.pending.count / 2
    if availFrames == 0 {
        ioNumPackets.pointee = 0
        return srcPause                      // partial output; converter state kept
    }
    let give = min(Int(ioNumPackets.pointee), availFrames, s.scratch.count / 2)
    s.scratch.withUnsafeMutableBytes { raw in
        s.pending.withUnsafeBytes { src in
            _ = memcpy(raw.baseAddress!, src.baseAddress!, give * 2)
        }
        ioData.pointee.mBuffers.mData = raw.baseAddress
    }
    s.pending.removeFirst(give * 2)
    ioData.pointee.mBuffers.mDataByteSize = UInt32(give * 2)
    ioData.pointee.mBuffers.mNumberChannels = 1
    ioNumPackets.pointee = UInt32(give)
    return noErr
}

Thread.detachNewThread {
    var outBuf = [UInt8](repeating: 0, count: 1 << 16)
    while true {
        G.micSem.wait()
        srcState.pending.append(G.micFIFO.popAll())
        while srcState.pending.count >= 2 {
            var outPackets = UInt32(outBuf.count / 2)
            var produced = Data()
            let st: OSStatus = outBuf.withUnsafeMutableBytes { raw in
                var abl = AudioBufferList(
                    mNumberBuffers: 1,
                    mBuffers: AudioBuffer(mNumberChannels: 1,
                                          mDataByteSize: UInt32(raw.count),
                                          mData: raw.baseAddress))
                let st = AudioConverterFillComplexBuffer(
                    srcConv, srcInputProc,
                    Unmanaged.passUnretained(srcState).toOpaque(),
                    &outPackets, &abl, nil)
                if outPackets > 0 {
                    produced = Data(bytes: raw.baseAddress!, count: Int(outPackets) * 2)
                }
                return st
            }
            if !produced.isEmpty {
                do { try stdoutFH.write(contentsOf: produced) } catch { exit(0) }
            }
            if st == srcPause { break }      // drained; wait for more mic audio
            if st != noErr { elog("SRC error: \(st)"); break }
        }
    }
}

elog("ready aec=\(!noAEC) mic out 16000Hz int16 mono; play in 24000Hz int16 mono (VPIO direct)")

// ── stdin: the playback/control loop (main thread) ───────────────────────────
let stdinFH = FileHandle.standardInput

func readExactly(_ n: Int) -> Data? {
    var d = Data(capacity: n)
    while d.count < n {
        guard let chunk = try? stdinFH.read(upToCount: n - d.count), !chunk.isEmpty
        else { return nil }
        d.append(chunk)
    }
    return d
}

loop: while true {
    guard let hdr = readExactly(5) else { break }
    let n = Int(hdr[1]) | Int(hdr[2]) << 8 | Int(hdr[3]) << 16 | Int(hdr[4]) << 24
    switch hdr[0] {
    case UInt8(ascii: "A"):
        guard n > 0, n % 2 == 0, let payload = readExactly(n) else { break loop }
        G.playFIFO.push(payload)
    case UInt8(ascii: "F"):
        if n > 0 { _ = readExactly(n) }
        G.playFIFO.clear()
    case UInt8(ascii: "P"):
        if n > 0 { _ = readExactly(n) }
        G.playFIFO.clear()
        AudioOutputUnitStop(G.au)
        elog("audio unit PAUSED — mic/playback off, device released for other apps")
    case UInt8(ascii: "R"):
        if n > 0 { _ = readExactly(n) }
        AudioOutputUnitStart(G.au)
        elog("audio unit RESUMED")
    default:
        elog("unknown frame type \(hdr[0]) — protocol desync, exiting")
        break loop
    }
}

AudioOutputUnitStop(au)
AudioUnitUninitialize(au)
