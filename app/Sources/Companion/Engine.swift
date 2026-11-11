import Foundation
import Observation

/// Runs `companion --json` and keeps what the panel shows, from its events (see `companion --help`).
@Observable @MainActor
final class Engine {
    struct Turn: Decodable, Hashable {
        var speaker, settled, tentative: String
    }

    private struct Event: Decodable {
        var type: String
        var text: String?
        var seconds: Int?
        var turns: [Turn]?
    }

    var running = false
    var ready = false
    var busy = false
    var status = "Not listening"
    var captions: [Turn] = []
    var question = ""
    var steps: [String] = []
    var answer: [String] = []
    var notesPath: String?
    var onExit: (() -> Void)?

    private var process: Process?
    private var input: FileHandle?
    private var errors: [String] = []

    func start() {
        guard !running else { return }
        let defaults = UserDefaults.standard
        var args = ["--json"]
        if defaults.bool(forKey: "micOnly") { args.append("--mic-only") }
        if let about = defaults.string(forKey: "about"), !about.isEmpty { args += ["--about", about] }
        args.append(defaults.string(forKey: "scope") ?? Settings.defaultScope)

        let process = Process(), stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
        // An interactive login shell, so PATH (uv, claude, gh) and a `companion` wrapper function in ~/.zshrc
        // (e.g. one that sets the Jira env) work exactly as in the terminal. The app's own copy of companion
        // goes first on PATH, after ~/.zshrc, so it's the one that runs, wrapper or not.
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-lic", "export PATH=\"$COMPANION_APP_DIR:$PATH\"; companion \"$@\"", "zsh"] + args
        var env = ProcessInfo.processInfo.environment
        env["COMPANION_APP_DIR"] = Bundle.main.resourcePath
        env["DISABLE_AUTO_UPDATE"] = "true"  // oh-my-zsh's update prompt would read the command pipe
        process.environment = env
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = stderr
        process.terminationHandler = { finished in
            Task { @MainActor in self.exited(finished.terminationStatus) }
        }

        captions = []; question = ""; steps = []; answer = []; errors = []
        status = "Starting…"
        do {
            try process.run()
        } catch {
            status = "Couldn't start zsh: \(error.localizedDescription)"
            return
        }
        self.process = process
        input = stdin.fileHandleForWriting
        running = true

        readLines(stdout) { self.handle($0) }
        readLines(stderr) { self.errors = Array((self.errors + [$0]).suffix(5)) }
    }

    /// `FileHandle.bytes.lines` holds small pipe writes back until its buffer fills, which stalls the
    /// events; readabilityHandler gets each write as it happens.
    private func readLines(_ pipe: Pipe, _ onLine: @escaping @MainActor (String) -> Void) {
        nonisolated(unsafe) var buffer = Data()  // only touched by the handler, which runs serially
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            buffer.append(chunk)
            var lines: [String] = []
            while let end = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                lines.append(String(decoding: buffer[..<end], as: UTF8.self))
                buffer.removeSubrange(...end)
            }
            DispatchQueue.main.async {  // FIFO, so events keep their order
                MainActor.assumeIsolated { lines.forEach(onLine) }
            }
        }
    }

    /// Same syntax as the terminal: "" answers the latest question, "60", focus text, "? typed question".
    func send(_ command: String) {
        guard ready, !busy, let input else { return }
        question = ""; steps = []; answer = []
        busy = true
        input.write(Data((command + "\n").utf8))
    }

    /// Ends the meeting: companion writes the summary, then exits.
    func stop() {
        guard let input else { return }
        question = ""; steps = []; answer = []
        busy = true
        try? input.close()  // EOF quits, like q
        self.input = nil
    }

    private func handle(_ line: String) {
        // Shell startup noise from ~/.zshrc isn't JSON; skip it
        guard let event = try? JSONDecoder().decode(Event.self, from: Data(line.utf8)) else { return }
        switch event.type {
        case "ready":  // sent each time companion waits for a command, also after "nothing heard yet"
            ready = true
            busy = false
        case "captions":
            captions = event.turns ?? []
        case "status":
            let text = event.text ?? ""
            status = text
            if text.hasPrefix("notes: ") { notesPath = String(text.dropFirst(7)) }
        case "question":
            question = event.text ?? ""
        case "step":
            steps.append(String(format: "%3ds  %@", event.seconds ?? 0, event.text ?? ""))
        case "line":
            answer.append(event.text ?? "")
        case "done":
            status = "answered in \(event.seconds ?? 0)s"
        default:
            break
        }
    }

    private func exited(_ code: Int32) {
        if code != 0 {
            status = code == 127 ? "Couldn't run companion. Is uv installed and on your PATH?"
                                 : (errors.last ?? "companion exited (\(code))")
        } else if notesPath == nil {
            status = "Not listening"
        }
        running = false; ready = false; busy = false
        process = nil; input = nil
        onExit?()
    }
}

enum Settings {
    static let defaultScope = NSHomeDirectory() + "/dev"
}
