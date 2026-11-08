import AppKit
import AVFoundation
import CoreFoundation
import Foundation

private let requestLimit = 32 * 1024
private let responseLimit = 3 * 1024 * 1024
private let pcmLimit = 1_920_000
private let allowedErrors: Set<String> = ["native_unavailable", "permission_denied", "unsupported_device", "invalid_request", "outcome_unknown"]

private struct NativeFailure: Error { let code: String }
private func fail(_ code: String) -> NativeFailure { NativeFailure(code: allowedErrors.contains(code) ? code : "native_unavailable") }
private func isBool(_ value: Any) -> Bool {
    guard let number = value as? NSNumber else { return false }
    return CFGetTypeID(number) == CFBooleanGetTypeID()
}
private func exactInt(_ value: Any?) -> Int? {
    guard let value = value, !isBool(value), let number = value as? NSNumber,
          CFNumberIsFloatType(number) == false else { return nil }
    return number.intValue
}
private func jsonObject(_ data: Data) throws -> [String: Any] {
    guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw fail("invalid_request") }
    return value
}
private struct DuplicateKeyScanner {
    private let bytes: [UInt8]
    private var index = 0

    init(_ data: Data) { bytes = Array(data) }

    mutating func containsDuplicateOrInvalidStructure() -> Bool {
        do {
            try parseValue(depth: 0)
            skipWhitespace()
            return index != bytes.count
        } catch { return true }
    }

    private mutating func skipWhitespace() {
        while index < bytes.count && (bytes[index] == 0x20 || bytes[index] == 0x09 || bytes[index] == 0x0a || bytes[index] == 0x0d) { index += 1 }
    }

    private mutating func parseString() throws -> String {
        guard index < bytes.count, bytes[index] == 0x22 else { throw fail("invalid_request") }
        let start = index; index += 1
        while index < bytes.count {
            if bytes[index] == 0x5c { index += 2; continue }
            if bytes[index] == 0x22 {
                index += 1
                let token = Data(bytes[start..<index])
                guard let value = try JSONSerialization.jsonObject(with: token, options: [.fragmentsAllowed]) as? String else { throw fail("invalid_request") }
                return value
            }
            index += 1
        }
        throw fail("invalid_request")
    }

    private mutating func parseValue(depth: Int) throws {
        guard depth < 64 else { throw fail("invalid_request") }
        skipWhitespace()
        guard index < bytes.count else { throw fail("invalid_request") }
        switch bytes[index] {
        case 0x22: _ = try parseString()
        case 0x7b:
            index += 1; skipWhitespace()
            if index < bytes.count && bytes[index] == 0x7d { index += 1; return }
            var keys = Set<String>()
            while true {
                skipWhitespace(); let key = try parseString()
                guard keys.insert(key).inserted else { throw fail("invalid_request") }
                skipWhitespace(); guard index < bytes.count && bytes[index] == 0x3a else { throw fail("invalid_request") }
                index += 1; try parseValue(depth: depth + 1); skipWhitespace()
                guard index < bytes.count else { throw fail("invalid_request") }
                if bytes[index] == 0x7d { index += 1; return }
                guard bytes[index] == 0x2c else { throw fail("invalid_request") }
                index += 1
            }
        case 0x5b:
            index += 1; skipWhitespace()
            if index < bytes.count && bytes[index] == 0x5d { index += 1; return }
            while true {
                try parseValue(depth: depth + 1); skipWhitespace()
                guard index < bytes.count else { throw fail("invalid_request") }
                if bytes[index] == 0x5d { index += 1; return }
                guard bytes[index] == 0x2c else { throw fail("invalid_request") }
                index += 1
            }
        default:
            let start = index
            while index < bytes.count && ![0x20, 0x09, 0x0a, 0x0d, 0x2c, 0x5d, 0x7d].contains(bytes[index]) { index += 1 }
            guard index > start else { throw fail("invalid_request") }
        }
    }
}
private func exactKeys(_ object: [String: Any], _ keys: Set<String>) -> Bool { Set(object.keys) == keys }
private func requestSchema(_ request: [String: Any]) throws -> (String, [String: Any]) {
    guard exactKeys(request, ["op", "arguments"]), let op = request["op"] as? String,
          let args = request["arguments"] as? [String: Any] else { throw fail("invalid_request") }
    switch op {
    case "capture_command":
        guard exactKeys(args, ["max_seconds"]), exactInt(args["max_seconds"]) == 30 else { throw fail("invalid_request") }
    default: throw fail("invalid_request")
    }
    return (op, args)
}

// Accepts converted mono 16 kHz samples only and has no device or permission
// dependencies, so endpointing and bounds are self-testable.
private struct CommandCaptureState {
    enum End: Equatable { case silence, limit, noSpeech, startupTimeout, invalidData }
    struct Update { let becameReady: Bool; let end: End? }

    static let sampleRate = 16_000
    static let frameSamples = 320                 // 20 ms
    static let calibrationSamples = 4_800         // 300 ms of actual input
    static let prerollLimit = 8_000                // 500 ms
    static let startVoiceSamples = 2_560           // 160 ms voiced, gaps tolerated
    static let candidateGapSamples = 1_600         // 100 ms
    static let trailingQuietSamples = 19_200       // 1.2 s
    static let noSpeechSeconds = 10.0              // bounded idle after first input
    static let startupSeconds = 5.0
    static let sessionSeconds = 30.0
    static let sampleLimit = 480_000              // 30 s at 16 kHz

    private let engineStartedAt: TimeInterval
    private(set) var firstInputAt: TimeInterval?
    private(set) var ready = false
    private(set) var end: End?
    private(set) var speaking = false
    private var partial = [Float]()
    private var preroll = [Float]()
    private(set) var recorded = [Float]()
    private var calibrationRMS = [Double]()
    private var calibratedSamples = 0
    private var startThreshold = 0.0025
    private var endThreshold = 0.0012
    private var candidateVoiced = 0
    private var candidateGap = 0
    private var candidateSpan = 0
    private var quietSamples = 0

    init(engineStartedAt: TimeInterval) { self.engineStartedAt = engineStartedAt }

    mutating func advance(now: TimeInterval) -> Update {
        guard end == nil else { return Update(becameReady: false, end: end) }
        if let firstInputAt = firstInputAt {
            let elapsed = max(0, now - firstInputAt)
            if elapsed >= Self.sessionSeconds { finish(speaking ? .limit : .noSpeech) }
            else if !speaking && elapsed >= Self.noSpeechSeconds { finish(.noSpeech) }
        } else if now - engineStartedAt >= Self.startupSeconds {
            finish(.startupTimeout)
        }
        return Update(becameReady: false, end: end)
    }

    mutating func append(_ samples: [Float], now: TimeInterval) -> Update {
        guard end == nil else { return Update(becameReady: false, end: end) }
        let timerUpdate = advance(now: now)
        guard end == nil else { return timerUpdate }
        guard !samples.isEmpty else { return Update(becameReady: false, end: nil) }
        if firstInputAt == nil { firstInputAt = now }

        var becameReady = false
        for sample in samples {
            guard sample.isFinite, abs(sample) <= 16 else {
                finish(.invalidData)
                break
            }
            partial.append(sample)
            if partial.count == Self.frameSamples {
                let frame = partial
                partial.removeAll(keepingCapacity: true)
                if process(frame) { becameReady = true }
            }
            if end != nil { break }
        }
        return Update(becameReady: becameReady, end: end)
    }

    private mutating func process(_ frame: [Float]) -> Bool {
        var energy = 0.0
        for sample in frame { energy += Double(sample) * Double(sample) }
        let rms = sqrt(energy / Double(frame.count))

        if !ready {
            keepPreroll(frame)
            calibrationRMS.append(rms)
            calibratedSamples += frame.count
            guard calibratedSamples >= Self.calibrationSamples else { return false }
            let sorted = calibrationRMS.sorted()
            let lowPercentileIndex = min(sorted.count - 1, Int(Double(sorted.count - 1) * 0.2))
            let noiseFloor = sorted[lowPercentileIndex]
            // Keep the learned background below both thresholds. Absolute caps
            // could place the stop threshold below steady noise, preventing an
            // utterance from ever ending. Floors retain quiet-room sensitivity;
            // the lower stop multiplier preserves start/stop hysteresis.
            startThreshold = max(0.0025, noiseFloor * 2.5)
            endThreshold = max(0.0012, noiseFloor * 1.5)
            ready = true
            return true
        }

        if speaking {
            appendRecorded(frame)
            if end != nil { return false }
            if rms < endThreshold { quietSamples += frame.count }
            else { quietSamples = 0 }
            if quietSamples >= Self.trailingQuietSamples { finish(.silence) }
            return false
        }

        keepPreroll(frame)
        if rms >= startThreshold {
            let nextSpan = candidateSpan + frame.count
            if candidateVoiced > 0 && nextSpan > Self.prerollLimit {
                candidateVoiced = frame.count
                candidateSpan = frame.count
            } else {
                candidateVoiced += frame.count
                candidateSpan = nextSpan
            }
            candidateGap = 0
            if candidateVoiced >= Self.startVoiceSamples {
                speaking = true
                recorded = preroll
                candidateVoiced = 0
                candidateGap = 0
                candidateSpan = 0
            }
        } else if candidateVoiced > 0 {
            candidateGap += frame.count
            candidateSpan += frame.count
            if candidateGap > Self.candidateGapSamples || candidateSpan > Self.prerollLimit {
                candidateVoiced = 0
                candidateGap = 0
                candidateSpan = 0
            }
        }
        return false
    }

    private mutating func keepPreroll(_ frame: [Float]) {
        preroll.append(contentsOf: frame)
        if preroll.count > Self.prerollLimit {
            preroll.removeFirst(preroll.count - Self.prerollLimit)
        }
    }

    private mutating func appendRecorded(_ frame: [Float]) {
        let room = Self.sampleLimit - recorded.count
        if room <= 0 { finish(.limit); return }
        recorded.append(contentsOf: frame.prefix(room))
        if recorded.count >= Self.sampleLimit { finish(.limit) }
    }

    private mutating func finish(_ reason: End) {
        guard end == nil else { return }
        end = reason
        partial.removeAll(keepingCapacity: false)
        preroll.removeAll(keepingCapacity: false)
        calibrationRMS.removeAll(keepingCapacity: false)
    }
}

private func commandCaptureSelfTest() -> Bool {
    let frame = CommandCaptureState.frameSamples
    var readiness = CommandCaptureState(engineStartedAt: 0)
    let beforeCalibration = readiness.append([Float](repeating: 0, count: 4_799), now: 0.1)
    guard !beforeCalibration.becameReady, beforeCalibration.end == nil,
          readiness.append([0], now: 0.2).becameReady, readiness.ready else { return false }

    var impulse = CommandCaptureState(engineStartedAt: 0)
    guard impulse.append([Float](repeating: 0, count: CommandCaptureState.calibrationSamples), now: 0.1).becameReady else { return false }
    var click = [Float](repeating: 0, count: frame); click[0] = 0.5
    _ = impulse.append(click, now: 0.2)
    for _ in 0..<6 { _ = impulse.append([Float](repeating: 0, count: frame), now: 0.22) }
    guard !impulse.speaking, impulse.recorded.isEmpty,
          impulse.advance(now: 10.1).end == .noSpeech else { return false }
    guard let emptyResponse = try? commandCaptureResult(impulse),
          Set(emptyResponse.keys) == Set(["sample_rate", "pcm_f32le", "audio_seconds", "reason"]),
          emptyResponse["reason"] as? String == "no_speech",
          emptyResponse["pcm_f32le"] as? String == "",
          emptyResponse["audio_seconds"] as? Double == 0 else { return false }
    let countAfterImpulseEnd = impulse.recorded.count
    _ = impulse.append([Float](repeating: 0.006, count: frame * 10), now: 10.2)
    guard impulse.end == .noSpeech, impulse.recorded.count == countAfterImpulseEnd else { return false }

    var gappedOnset = CommandCaptureState(engineStartedAt: 0)
    guard gappedOnset.append([Float](repeating: 0, count: CommandCaptureState.calibrationSamples), now: 0.1).becameReady else { return false }
    _ = gappedOnset.append([Float](repeating: 0.006, count: frame * 4), now: 0.2)
    _ = gappedOnset.append([Float](repeating: 0, count: frame * 3), now: 0.26)
    _ = gappedOnset.append([Float](repeating: 0.006, count: frame * 4), now: 0.34)
    guard gappedOnset.speaking else { return false }

    var utterance = CommandCaptureState(engineStartedAt: 0)
    guard utterance.append([Float](repeating: 0, count: CommandCaptureState.calibrationSamples), now: 0.1).becameReady else { return false }
    _ = utterance.append([Float](repeating: 0.006, count: frame * 15), now: 0.4)
    guard utterance.speaking, utterance.recorded.count == 9_600,
          utterance.recorded.prefix(100).allSatisfy({ $0 == 0 }) else { return false }
    _ = utterance.append([Float](repeating: 0, count: frame * 25), now: 0.9)
    guard utterance.end == nil else { return false }
    _ = utterance.append([Float](repeating: 0.006, count: frame * 15), now: 1.2)
    guard utterance.end == nil else { return false }
    _ = utterance.append([Float](repeating: 0, count: CommandCaptureState.trailingQuietSamples), now: 2.4)
    guard utterance.end == .silence, utterance.recorded.contains(where: { abs($0) > 0.005 }) else { return false }
    guard let speechResponse = try? commandCaptureResult(utterance),
          Set(speechResponse.keys) == Set(["sample_rate", "pcm_f32le", "audio_seconds", "reason"]),
          speechResponse["reason"] as? String == "silence",
          let speechPCM = speechResponse["pcm_f32le"] as? String,
          let speechBytes = Data(base64Encoded: speechPCM),
          speechBytes.count == utterance.recorded.count * MemoryLayout<Float>.size,
          speechBytes.count <= pcmLimit else { return false }
    let silenceCount = utterance.recorded.count
    _ = utterance.append([Float](repeating: 0.006, count: frame), now: 2.5)
    guard utterance.recorded.count == silenceCount else { return false }

    var bounded = CommandCaptureState(engineStartedAt: 0)
    guard bounded.append([Float](repeating: 0, count: CommandCaptureState.calibrationSamples), now: 0.1).becameReady else { return false }
    _ = bounded.append([Float](repeating: 0.006, count: CommandCaptureState.sampleLimit), now: 0.2)
    guard bounded.end == .limit, bounded.recorded.count == CommandCaptureState.sampleLimit else { return false }

    var invalid = CommandCaptureState(engineStartedAt: 0)
    _ = invalid.append([Float](repeating: 0, count: CommandCaptureState.calibrationSamples), now: 0.1)
    let invalidUpdate = invalid.append([Float](repeating: .nan, count: 1), now: 0.2)
    guard invalidUpdate.end == .invalidData, invalid.ready else { return false }
    _ = invalid.append([Float](repeating: 0.006, count: frame * 20), now: 0.3)
    guard invalid.end == .invalidData, invalid.recorded.isEmpty else { return false }

    var startup = CommandCaptureState(engineStartedAt: 0)
    guard startup.advance(now: 4.999).end == nil,
          startup.advance(now: 5).end == .startupTimeout else { return false }
    var incompleteInput = CommandCaptureState(engineStartedAt: 0)
    _ = incompleteInput.append([0], now: 0.1)
    guard incompleteInput.advance(now: 10.2).end == .noSpeech,
          (try? commandCaptureResult(incompleteInput)) == nil else { return false }
    var hysteresis = CommandCaptureState(engineStartedAt: 0)
    guard hysteresis.append([Float](repeating: 0.002, count: CommandCaptureState.calibrationSamples), now: 0.1).becameReady else { return false }
    _ = hysteresis.append([Float](repeating: 0.009, count: frame * 10), now: 0.4)
    _ = hysteresis.append([Float](repeating: 0.006, count: frame * 80), now: 2.0)
    guard hysteresis.speaking, hysteresis.end == nil else { return false }
    var session = CommandCaptureState(engineStartedAt: 0)
    guard session.append([Float](repeating: 0, count: CommandCaptureState.calibrationSamples), now: 0.1).becameReady,
          session.append([Float](repeating: 0.006, count: frame * 10), now: 0.4).end == nil,
          session.advance(now: 30.1).end == .limit else { return false }
    return commandBackgroundSelfTest()
}

// Exercise calibrated ambient levels above the former absolute stop/start caps.
// These zero-mean synthetic samples never use a microphone or an audio engine.
private func commandBackgroundSelfTest() -> Bool {
    func wave(_ amplitude: Float, count: Int) -> [Float] {
        return (0..<count).map { ($0 / 10) % 2 == 0 ? amplitude : -amplitude }
    }
    func feed(_ state: inout CommandCaptureState, amplitude: Float, frames: Int, clock: inout Double) {
        let samples = wave(amplitude, count: CommandCaptureState.frameSamples)
        for _ in 0..<frames {
            clock += 0.02
            _ = state.append(samples, now: clock)
        }
    }
    for background in [Float(0.0005), Float(0.006), Float(0.02)] {
        let speech: Float = background == 0.02 ? 0.08 : 0.04
        var clock = 0.0
        var utterance = CommandCaptureState(engineStartedAt: 0)
        feed(&utterance, amplitude: background, frames: 15, clock: &clock)
        guard utterance.ready else { return false }
        feed(&utterance, amplitude: background, frames: 50, clock: &clock)
        guard !utterance.speaking, utterance.recorded.isEmpty else { return false }
        feed(&utterance, amplitude: speech, frames: 25, clock: &clock)
        guard utterance.speaking, utterance.end == nil else { return false }
        feed(&utterance, amplitude: background, frames: 25, clock: &clock)
        guard utterance.end == nil else { return false } // Natural pause, not an endpoint.
        feed(&utterance, amplitude: speech, frames: 15, clock: &clock)
        feed(&utterance, amplitude: background, frames: 59, clock: &clock)
        guard utterance.end == nil else { return false }
        feed(&utterance, amplitude: background, frames: 1, clock: &clock)
        guard utterance.end == .silence else { return false }

        var idle = CommandCaptureState(engineStartedAt: 0)
        clock = 0
        feed(&idle, amplitude: background, frames: 100, clock: &clock)
        guard !idle.speaking, idle.recorded.isEmpty,
              idle.advance(now: 10.1).end == .noSpeech else { return false }

        var bounded = CommandCaptureState(engineStartedAt: 0)
        _ = bounded.append(wave(background, count: CommandCaptureState.calibrationSamples), now: 0.1)
        _ = bounded.append(wave(speech, count: CommandCaptureState.sampleLimit), now: 0.4)
        guard bounded.end == .limit, bounded.recorded.count == CommandCaptureState.sampleLimit else { return false }
    }

    // Signals below a loud learned background must not falsely start a command.
    var belowNoise = CommandCaptureState(engineStartedAt: 0)
    var clock = 0.0
    feed(&belowNoise, amplitude: 0.02, frames: 15, clock: &clock)
    feed(&belowNoise, amplitude: 0.009, frames: 10, clock: &clock)
    feed(&belowNoise, amplitude: 0.006, frames: 80, clock: &clock)
    return !belowNoise.speaking && belowNoise.recorded.isEmpty && belowNoise.advance(now: 10.1).end == .noSpeech
}

private func commandCaptureResult(_ state: CommandCaptureState) throws -> [String: Any] {
    guard state.ready else { throw fail("native_unavailable") }
    guard let end = state.end else { throw fail("native_unavailable") }
    if end == .noSpeech {
        return ["sample_rate": 16000, "pcm_f32le": "", "audio_seconds": 0.0, "reason": "no_speech"]
    }
    let reason: String
    switch end {
    case .silence: reason = "silence"
    case .limit: reason = "limit"
    default: throw fail("native_unavailable")
    }
    let samples = state.recorded
    guard state.speaking, !samples.isEmpty, samples.count <= CommandCaptureState.sampleLimit,
          samples.allSatisfy({ $0.isFinite && abs($0) <= 16 }) else { throw fail("native_unavailable") }
    var pcm = Data(capacity: samples.count * MemoryLayout<Float>.size)
    for sample in samples {
        var bits = sample.bitPattern.littleEndian
        withUnsafeBytes(of: &bits) { pcm.append(contentsOf: $0) }
    }
    guard pcm.count <= pcmLimit else { throw fail("outcome_unknown") }
    return ["sample_rate": 16000, "pcm_f32le": pcm.base64EncodedString(),
            "audio_seconds": Double(samples.count) / 16000, "reason": reason]
}

private func captureCommand() throws -> [String: Any] {
    let status = AVCaptureDevice.authorizationStatus(for: .audio)
    guard status != .denied && status != .restricted else { throw fail("permission_denied") }
    let granted = DispatchSemaphore(value: 0); var permitted = false
    AVCaptureDevice.requestAccess(for: .audio) { value in permitted = value; granted.signal() }
    guard granted.wait(timeout: .now() + 30) == .success, permitted else { throw fail("permission_denied") }

    let engine = AVAudioEngine(); let input = engine.inputNode
    let inputFormat = input.inputFormat(forBus: 0)
    guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
          let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false),
          let converter = AVAudioConverter(from: inputFormat, to: target) else { throw fail("unsupported_device") }

    let lock = NSLock()
    let finished = DispatchSemaphore(value: 0)
    var state = CommandCaptureState(engineStartedAt: ProcessInfo.processInfo.systemUptime)
    var done = false
    var cancelled = false
    var conversionFailed = false
    input.installTap(onBus: 0, bufferSize: 2048, format: inputFormat) { buffer, _ in
        lock.lock()
        guard !done else { lock.unlock(); return }
        guard let converted = AVAudioPCMBuffer(pcmFormat: target,
                                               frameCapacity: AVAudioFrameCount(Double(buffer.frameLength) * 16000 / inputFormat.sampleRate + 64)) else {
            conversionFailed = true; done = true; finished.signal()
            lock.unlock()
            return
        }
        var supplied = false; var conversionError: NSError?
        let conversionStatus = converter.convert(to: converted, error: &conversionError) { _, outStatus in
            if supplied { outStatus.pointee = .noDataNow; return nil }
            supplied = true; outStatus.pointee = .haveData; return buffer
        }
        guard conversionError == nil, conversionStatus != .error,
              let channel = converted.floatChannelData?[0] else {
            conversionFailed = true; done = true; finished.signal()
            lock.unlock()
            return
        }
        let count = Int(converted.frameLength)
        guard count > 0 else { lock.unlock(); return }
        guard count <= 16_000 else {
            conversionFailed = true; done = true; finished.signal()
            lock.unlock()
            return
        }
        let samples = Array(UnsafeBufferPointer(start: channel, count: count))
        let update = state.append(samples, now: ProcessInfo.processInfo.systemUptime)
        if update.becameReady { output(["event": "ready"]) }
        if update.end != nil { done = true; finished.signal() }
        lock.unlock()
    }

    _ = signal(SIGTERM, SIG_IGN)
    let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global(qos: .userInitiated))
    termination.setEventHandler {
        lock.lock(); let shouldStop = !done; cancelled = true; done = true; lock.unlock()
        if shouldStop { finished.signal() }
    }
    termination.resume()
    var timer: DispatchSourceTimer?
    defer {
        timer?.cancel()
        termination.cancel()
        lock.lock(); done = true; lock.unlock()
        engine.stop()
        input.removeTap(onBus: 0)
        converter.reset()
    }

    do { try engine.start() } catch { throw fail("native_unavailable") }
    let activeTimer = DispatchSource.makeTimerSource(queue: .global(qos: .userInitiated))
    activeTimer.schedule(deadline: .now(), repeating: .milliseconds(25))
    activeTimer.setEventHandler {
        lock.lock()
        if !done {
            let update = state.advance(now: ProcessInfo.processInfo.systemUptime)
            if update.end != nil { done = true; finished.signal() }
        }
        lock.unlock()
    }
    activeTimer.resume(); timer = activeTimer

    let waitResult = finished.wait(timeout: .now() + 36)
    lock.lock()
    if waitResult != .success && !done { conversionFailed = true; done = true }
    let finalState = state
    let failed = conversionFailed
    let wasCancelled = cancelled
    done = true
    lock.unlock()
    guard waitResult == .success, !failed, !wasCancelled else { throw fail("native_unavailable") }
    return try commandCaptureResult(finalState)
}

private func execute(_ op: String, _ args: [String: Any]) throws -> [String: Any] {
    switch op {
    case "capture_command":
        return try captureCommand()
    default: throw fail("invalid_request")
    }
}

private func output(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), data.count <= responseLimit else {
        FileHandle.standardOutput.write(Data("{\"ok\":false,\"error\":\"outcome_unknown\"}\n".utf8)); return
    }
    FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data([10]))
}

let arguments = CommandLine.arguments
if arguments.count == 2 && arguments[1] == "--help" {
    print("voice-native: read one bounded JSON request from stdin; the fixed capture_command operation is schema-validated. --validate checks a request without performing it; --self-test exercises only the command-capture state machine.")
    exit(0)
}
if arguments.count == 2 && arguments[1] == "--self-test" {
    if commandCaptureSelfTest() { print("command capture self-test passed"); exit(0) }
    print("command capture self-test failed"); exit(1)
}
let validateOnly = arguments.count == 2 && arguments[1] == "--validate"
guard arguments.count == 1 || validateOnly else { output(["ok": false, "error": "invalid_request"]); exit(2) }
var input = Data()
while input.count <= requestLimit {
    let next = FileHandle.standardInput.readData(ofLength: min(4096, requestLimit + 1 - input.count))
    if next.isEmpty { break }
    input.append(next)
}
private var duplicateScanner = DuplicateKeyScanner(input)
guard !input.isEmpty, input.count <= requestLimit, !duplicateScanner.containsDuplicateOrInvalidStructure(),
      let request = try? jsonObject(input), let (op, args) = try? requestSchema(request) else {
    output(["ok": false, "error": "invalid_request"]); exit(0)
}
if validateOnly {
    output(["ok": true, "result": ["valid": true]]); exit(0)
}
do {
    // Establish the bundled application context before microphone authorization.
    _ = NSApplication.shared
    let result = try execute(op, args)
    output(["ok": true, "result": result])
} catch let error as NativeFailure {
    output(["ok": false, "error": error.code])
} catch {
    output(["ok": false, "error": "native_unavailable"])
}
