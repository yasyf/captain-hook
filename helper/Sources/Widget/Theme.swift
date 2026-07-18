import SwiftUI

enum Theme {
    static let healthy = Color(.sRGB, red: 0.30, green: 0.80, blue: 0.49)
    static let failing = Color(.sRGB, red: 0.93, green: 0.25, blue: 0.21)
    static let accent = Color(.sRGB, red: 0.25, green: 0.48, blue: 0.98)

    static var background: some View {
        LinearGradient(
            colors: [
                Color(.sRGB, red: 0.09, green: 0.11, blue: 0.17),
                Color(.sRGB, red: 0.05, green: 0.06, blue: 0.10),
            ],
            startPoint: .top,
            endPoint: .bottom
        )
    }

    static func healthColor(_ ok: Bool) -> Color { ok ? healthy : failing }
}
