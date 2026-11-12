import SwiftUI

struct PanelView: View {
    let engine: Engine
    @State private var input = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            captions
                .frame(maxWidth: .infinity, minHeight: 64, alignment: .bottomLeading)
            Divider()

            ScrollView {
                if !engine.question.isEmpty || !engine.steps.isEmpty || !engine.answer.isEmpty {
                    answerCard
                }
            }
            .defaultScrollAnchor(.bottom)

            Text(engine.status).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            if engine.running {
                HStack(spacing: 6) {
                    if engine.busy { ProgressView().controlSize(.small) }  // so a slow answer doesn't look frozen
                    // Stays editable while answering, so the next question can be typed ahead
                    TextField("Ask, or press Enter for the latest question", text: $input)
                        .onSubmit { send() }
                        .disabled(!engine.ready)
                    Group {
                        // Same as typing "!" before the command: select a screen area, then ask with what's typed
                        Button { send("!") } label: { Image(systemName: "camera.viewfinder") }
                            .help("Select a screen area and ask with the screenshot")
                        Button { send("+") } label: { Image(systemName: "doc.on.clipboard") }
                            .help("Ask with the image on the clipboard, e.g. after ⌘⌃⇧4")
                    }
                    .disabled(!engine.ready || engine.busy)
                    Image(systemName: "questionmark.circle")
                        .foregroundStyle(.secondary)
                        .help("""
                            Enter: answer the latest question
                            60: answer about the last 60 seconds
                            text: answer focused on that text
                            ? question: ask a typed question
                            ! question: attach a screenshot
                            + question: attach the clipboard image
                            """)
                }
                .buttonStyle(.borderless)
            } else {
                Button("Start listening") { engine.start() }
            }
        }
        .padding()
        .frame(minWidth: 380, minHeight: 300)
    }

    private var answerCard: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(engine.question).font(.caption).foregroundStyle(.secondary)
                Spacer()
                if !engine.answer.isEmpty {
                    copyButton(engine.answer.joined(separator: "\n"))
                }
            }
            if engine.answer.isEmpty {
                steps  // live progress until the answer starts
            } else if !engine.steps.isEmpty {
                DisclosureGroup(engine.steps.count == 1 ? "1 step" : "\(engine.steps.count) steps") { steps }
                    .font(.caption).foregroundStyle(.secondary)
            }
            let blocks = self.blocks
            let headline = blocks.firstIndex { if case .text = $0 { return true } else { return false } }
            ForEach(Array(blocks.enumerated()), id: \.offset) { i, block in
                switch block {
                case .gap:
                    Color.clear.frame(height: 4)  // a paragraph break, not a full empty line
                case .text(let line):
                    render(line).bold(i == headline)
                case .code(let code):
                    HStack(alignment: .top, spacing: 4) {  // beside the code, so it never covers a long line
                        ScrollView(.horizontal) {
                            Text(verbatim: code).font(.callout.monospaced()).fixedSize().padding(8)
                        }
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 6))
                        copyButton(code).padding(.top, 8)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
        .padding(10)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 8))
    }

    private var steps: some View {
        ForEach(Array(engine.steps.enumerated()), id: \.offset) { _, step in
            Text(step).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1)
        }
    }

    private func copyButton(_ text: String) -> some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        } label: {
            Image(systemName: "doc.on.doc").font(.caption)
        }
        .buttonStyle(.borderless)
        .help("Copy")
    }

    /// Ignored while an answer is in progress, so what's typed ahead isn't lost.
    private func send(_ prefix: String = "") {
        guard !engine.busy else { return }
        engine.send(prefix + input)
        input = ""
    }

    /// The last caption turns, like a call's captions: words still in flux are dimmed.
    private var captions: some View {
        var text = Text("")
        for (i, turn) in engine.captions.suffix(3).enumerated() {
            if i > 0 { text = text + Text("\n") }
            if !turn.speaker.isEmpty {
                text = text + Text(turn.speaker + "  ").foregroundStyle(turn.speaker == "me" ? .cyan : .orange)
            }
            text = text + Text(turn.settled + " ") + Text(turn.tentative).foregroundStyle(.secondary)
        }
        return text.lineLimit(4)
    }

    private enum Block {
        case text(String), code(String), gap
    }

    /// The answer's lines, with each fenced code block kept as one block, exactly as written.
    private var blocks: [Block] {
        var blocks: [Block] = []
        var code: [String]?  // non-nil inside a fence
        for line in engine.answer {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if let lines = code {
                    blocks.append(.code(lines.joined(separator: "\n")))
                    code = nil
                } else {
                    code = []
                }
            } else if code != nil {
                code?.append(line)
            } else {
                blocks.append(trimmed.isEmpty ? .gap : .text(line))
            }
        }
        if let lines = code { blocks.append(.code(lines.joined(separator: "\n"))) }  // still streaming in
        return blocks
    }

    private func render(_ line: String) -> Text {
        var text = line.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("#") {
            return Text(text.drop { $0 == "#" }.trimmingCharacters(in: .whitespaces)).bold()
        }
        if text.hasPrefix("- ") || text.hasPrefix("* ") {
            text = "•  " + text.dropFirst(2)
        }
        let markdown = try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
        return Text(markdown ?? AttributedString(text))
    }
}
