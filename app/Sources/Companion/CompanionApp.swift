import AppKit
import Carbon
import SwiftUI

@main
struct CompanionApp: App {
    @NSApplicationDelegateAdaptor private var delegate: AppDelegate

    var body: some Scene {
        MenuBarExtra("Companion", systemImage: delegate.engine.running ? "waveform.circle.fill" : "waveform") {
            MenuView(engine: delegate.engine, showPanel: { delegate.showPanel(focus: true) })
        }
        .menuBarExtraStyle(.window)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let engine = Engine()
    private var panel: NSPanel!
    private var hotkey: Hotkey?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)  // menu bar only, no Dock icon
        panel = makePanel()
        hotkey = Hotkey { [weak self] in self?.hotkeyPressed() }
        if hotkey?.registered == false {
            engine.status = "⌘Return is taken by another app; use the panel's field instead"
        }
    }

    /// Quitting mid-meeting still writes the summary: stop, then quit once companion exits.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard engine.running else { return .terminateNow }
        engine.onExit = { NSApp.reply(toApplicationShouldTerminate: true) }
        engine.stop()
        return .terminateLater
    }

    private func hotkeyPressed() {
        if engine.ready {
            engine.send("")
        } else if !engine.running {
            engine.start()
        }
        showPanel(focus: false)
    }

    /// Without focus the panel shows up without taking the keyboard from the call app.
    func showPanel(focus: Bool) {
        focus ? panel.makeKeyAndOrderFront(nil) : panel.orderFrontRegardless()
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 420),
                            styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
                            backing: .buffered, defer: false)
        panel.title = "Companion"
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.contentView = NSHostingView(rootView: PanelView(engine: engine))
        if !panel.setFrameUsingName("panel"), let screen = NSScreen.main?.visibleFrame {
            panel.setFrameTopLeftPoint(NSPoint(x: screen.maxX - 480, y: screen.maxY - 20))
        }
        panel.setFrameAutosaveName("panel")
        return panel
    }
}

/// A global ⌘Return. Carbon hot keys need no Accessibility permission, unlike event taps.
final class Hotkey {
    private var ref: EventHotKeyRef?
    private let action: () -> Void
    private(set) var registered = false

    init(action: @escaping () -> Void) {
        self.action = action
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, userData in
            Unmanaged<Hotkey>.fromOpaque(userData!).takeUnretainedValue().action()
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)
        let id = EventHotKeyID(signature: OSType(0x434D_504E), id: 1)  // "CMPN"
        registered = RegisterEventHotKey(UInt32(kVK_Return), UInt32(cmdKey), id,
                                         GetApplicationEventTarget(), 0, &ref) == noErr
    }
}

struct MenuView: View {
    let engine: Engine
    let showPanel: () -> Void
    @AppStorage("scope") private var scope = Settings.defaultScope
    @AppStorage("about") private var about = ""
    @AppStorage("micOnly") private var micOnly = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(engine.running ? (engine.ready ? "Listening" : "Starting…") : "Not listening").font(.headline)

            Group {
                HStack {
                    Text((scope as NSString).abbreviatingWithTildeInPath).lineLimit(1).truncationMode(.head)
                    Spacer()
                    Button("Change…", action: chooseScope)
                }
                TextField("What's the meeting about? (optional)", text: $about)
                Toggle("Mic only (face-to-face meeting)", isOn: $micOnly)
            }
            .disabled(engine.running)

            HStack {
                if engine.running {
                    Button("Stop & summarize") { engine.stop() }
                } else {
                    Button("Start listening") {
                        engine.start()
                        showPanel()
                    }
                }
                Button("Show panel", action: showPanel)
            }
            if let notes = engine.notesPath, !engine.running {
                Button("Open meeting notes") { NSWorkspace.shared.open(URL(fileURLWithPath: notes)) }
            }

            Divider()
            Text("⌘Return answers the latest question").font(.caption).foregroundStyle(.secondary)
            Button("Quit Companion") { NSApp.terminate(nil) }
        }
        .padding()
        .frame(width: 300)
    }

    private func chooseScope() {
        let open = NSOpenPanel()
        open.canChooseDirectories = true
        open.canChooseFiles = false
        open.directoryURL = URL(fileURLWithPath: scope)
        NSApp.activate()
        if open.runModal() == .OK, let url = open.url { scope = url.path }
    }
}
