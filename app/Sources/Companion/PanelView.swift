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
                VStack(alignment: .leading, spacing: 4) {
                    if !engine.question.isEmpty {
                        Text(engine.question).italic()
                    }
                    ForEach(Array(engine.steps.enumerated()), id: \.offset) { _, step in
                        Text(step).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1)
                    }
                    let headline = engine.answer.firstIndex { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
                    ForEach(Array(engine.answer.enumerated()), id: \.offset) { i, line in
                        render(line).bold(i == headline)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
            }
            .defaultScrollAnchor(.bottom)

            Text(engine.status).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            if engine.running {
                TextField("Enter: latest question · 60 · focus text · ? typed question", text: $input)
                    .onSubmit {
                        engine.send(input)
                        input = ""
                    }
                    .disabled(!engine.ready || engine.busy)
            } else {
                Button("Start listening") { engine.start() }
            }
        }
        .padding()
        .frame(minWidth: 380, minHeight: 300)
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
