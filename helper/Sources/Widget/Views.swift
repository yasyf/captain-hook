import SwiftUI
import WidgetKit

struct StatusWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: StatusEntry

    var body: some View {
        switch entry.state {
        case .noFile:
            MessageView(symbol: "moon.zzz", title: "review not enabled", detail: "capt-hook review enable")
        case .denied:
            MessageView(symbol: "lock.slash", title: "can't read ~/.capt-hook", detail: "check widget entitlements")
        case .unreadable:
            MessageView(symbol: "exclamationmark.triangle", title: "status unreadable", detail: "version skew?")
        case .ok(let snapshot):
            content(snapshot, stale: false)
        case .stale(let snapshot):
            content(snapshot, stale: true)
        }
    }

    @ViewBuilder
    private func content(_ snapshot: Snapshot, stale: Bool) -> some View {
        Group {
            switch family {
            case .systemSmall:
                SmallView(snapshot: snapshot)
            default:
                PRListView(snapshot: snapshot, maxRows: family == .systemLarge ? 8 : 3)
            }
        }
        .opacity(stale ? 0.55 : 1)
    }
}

// Small: open-PR count + health dot, whole tile deep-links to the newest PR.
struct SmallView: View {
    let snapshot: Snapshot

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Image(systemName: "arrow.triangle.branch").foregroundStyle(Theme.accent)
                Spacer()
                Circle().fill(Theme.healthColor(snapshot.health.ok)).frame(width: 10, height: 10)
            }
            Spacer()
            Text("\(snapshot.openPRCount)")
                .font(.system(size: 40, weight: .bold, design: .rounded))
                .monospacedDigit()
            Text(snapshot.openPRCount == 1 ? "open PR" : "open PRs")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .widgetURL(firstPRURL(snapshot))
    }
}

// Medium/large: header + per-PR Link rows (each row deep-links to its PR).
struct PRListView: View {
    let snapshot: Snapshot
    let maxRows: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "arrow.triangle.branch").foregroundStyle(Theme.accent)
                Text("capt-hook review").font(.caption).bold()
                Spacer()
                Circle().fill(Theme.healthColor(snapshot.health.ok)).frame(width: 9, height: 9)
                Text("\(snapshot.openPRCount)").font(.caption).monospacedDigit().foregroundStyle(.secondary)
            }
            let prs = snapshot.allOpenPRs
            if prs.isEmpty {
                Spacer()
                Text("no open PRs").font(.callout).foregroundStyle(.secondary)
                Spacer()
            } else {
                ForEach(prs.prefix(maxRows)) { PRRow(pr: $0) }
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

struct PRRow: View {
    let pr: OpenPR

    var body: some View {
        if let url = validHTTPURL(pr.url) {
            Link(destination: url) { rowBody }
        } else {
            rowBody
        }
    }

    private var rowBody: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(pr.title).font(.caption).lineLimit(1)
            HStack(spacing: 4) {
                Text(pr.rule).font(.caption2).foregroundStyle(Theme.accent).lineLimit(1)
                Text(pr.openedAt, format: .relative(presentation: .named))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct MessageView: View {
    let symbol: String
    let title: String
    let detail: String

    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: symbol).font(.title2).foregroundStyle(.secondary)
            Text(title).font(.callout).bold().multilineTextAlignment(.center)
            Text(detail).font(.caption2).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

func validHTTPURL(_ string: String) -> URL? {
    isHTTPURL(string) ? URL(string: string) : nil
}

func firstPRURL(_ snapshot: Snapshot) -> URL? {
    snapshot.allOpenPRs.first.flatMap { validHTTPURL($0.url) }
}
