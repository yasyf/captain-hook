import Foundation
import WidgetKit

struct StatusEntry: TimelineEntry {
    let date: Date
    let state: State

    enum State {
        case ok(Snapshot)
        case stale(Snapshot) // generated_at older than staleAfter
        case noFile // review never enabled
        case denied // read refused — sandbox/entitlement, not a missing file
        case unreadable // decode failure or schema skew
    }
}

struct StatusProvider: TimelineProvider {
    static let staleAfter: TimeInterval = 30 * 60
    static let refreshWindow: TimeInterval = 15 * 60

    func placeholder(in _: Context) -> StatusEntry {
        StatusEntry(date: .now, state: .ok(.sample))
    }

    func getSnapshot(in context: Context, completion: @escaping (StatusEntry) -> Void) {
        completion(context.isPreview ? placeholder(in: context) : load(at: .now))
    }

    // One load, re-emitted at minute offsets so the relative ages stay honest;
    // staleness is judged per entry against its own display date.
    func getTimeline(in _: Context, completion: @escaping (Timeline<StatusEntry>) -> Void) {
        let now = Date()
        let base = load(at: now).state
        let entries = (0 ..< 15).map { minute -> StatusEntry in
            let date = now.addingTimeInterval(Double(minute) * 60)
            switch base {
            case .ok(let snap), .stale(let snap):
                let stale = date.timeIntervalSince(snap.generatedAt) > Self.staleAfter
                return StatusEntry(date: date, state: stale ? .stale(snap) : .ok(snap))
            default:
                return StatusEntry(date: date, state: base)
            }
        }
        completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(Self.refreshWindow))))
    }

    private func load(at now: Date) -> StatusEntry {
        let data: Data
        do {
            data = try Data(contentsOf: HelperPaths.status)
        } catch let error as CocoaError where error.code == .fileReadNoSuchFile {
            return StatusEntry(date: now, state: .noFile)
        } catch {
            return StatusEntry(date: now, state: .denied)
        }
        guard let snapshot = try? SnapshotContract.decode(data, using: .snapshot) else {
            return StatusEntry(date: now, state: .unreadable)
        }
        let stale = now.timeIntervalSince(snapshot.generatedAt) > Self.staleAfter
        return StatusEntry(date: now, state: stale ? .stale(snapshot) : .ok(snapshot))
    }
}
