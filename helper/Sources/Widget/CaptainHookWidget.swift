import SwiftUI
import WidgetKit

@main
struct CaptainHookWidgetBundle: WidgetBundle {
    var body: some Widget { CaptainHookWidget() }
}

struct CaptainHookWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "CaptainHook", provider: StatusProvider()) { entry in
            StatusWidgetView(entry: entry)
                .containerBackground(for: .widget) { Theme.background }
        }
        .configurationDisplayName("Captain Hook")
        .description("Open capt-hook review PRs and pipeline health.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}
