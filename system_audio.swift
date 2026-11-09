// Streams everything the Mac plays to stdout as 16 kHz mono float32, whatever the output device.
// Uses a Core Audio process tap (macOS 14.2+), so headphones work and no virtual driver is needed.
// macOS sends silence until the terminal app has System Audio Recording permission.

import CoreAudio
import Foundation

let outputRate = 16_000.0

func check(_ status: OSStatus, _ step: String) {
    guard status != noErr else { return }
    FileHandle.standardError.write(Data("system audio: \(step) failed (\(status))\n".utf8))
    exit(1)
}

let tap = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tap.name = "companion"
tap.isPrivate = true
tap.muteBehavior = .unmuted

var tapID = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateProcessTap(tap, &tapID), "creating the tap")

var format = AudioStreamBasicDescription()
var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
var formatAddress = AudioObjectPropertyAddress(
    mSelector: kAudioTapPropertyFormat,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
check(AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &formatSize, &format), "reading the tap format")

let aggregate: [String: Any] = [
    kAudioAggregateDeviceNameKey: "companion-system-audio",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceTapListKey: [
        [kAudioSubTapUIDKey: tap.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]
    ],
]
var device = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &device), "creating the capture device")

// Downsample by averaging each output sample's span of input frames; plenty for speech
let framesPerOutput = format.mSampleRate / outputRate
var position = 0.0
var sum: Float = 0
var count: Float = 0
var output = [Float]()

var ioProc: AudioDeviceIOProcID?
check(AudioDeviceCreateIOProcIDWithBlock(&ioProc, device, nil) { _, input, _, _, _ in
    let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
    guard let buffer = buffers.first, let data = buffer.mData else { return }
    let channels = Int(max(buffer.mNumberChannels, 1))
    let samples = data.assumingMemoryBound(to: Float.self)
    let frames = Int(buffer.mDataByteSize) / (MemoryLayout<Float>.size * channels)

    output.removeAll(keepingCapacity: true)
    for frame in 0..<frames {
        for channel in 0..<channels { sum += samples[frame * channels + channel] / Float(channels) }
        count += 1
        position += 1
        if position >= framesPerOutput {
            position -= framesPerOutput
            output.append(sum / count)
            sum = 0
            count = 0
        }
    }
    output.withUnsafeBytes { bytes in
        fwrite(bytes.baseAddress, 1, bytes.count, stdout)
        fflush(stdout)
    }
}, "creating the audio callback")

check(AudioDeviceStart(device, ioProc), "starting capture")

// The callback only runs while audio plays, so a closed stdout can go unnoticed. stdin closes
// as soon as the companion exits, even after a crash, so capture never outlives it.
DispatchQueue.global().async {
    _ = FileHandle.standardInput.readDataToEndOfFile()
    _exit(0)
}
dispatchMain()
