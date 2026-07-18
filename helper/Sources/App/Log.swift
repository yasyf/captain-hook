import os

enum Log {
    private static let subsystem = "com.yasyf.capt-hook.helper"
    static let app = Logger(subsystem: subsystem, category: "app")
    static let socket = Logger(subsystem: subsystem, category: "socket")
    static let notify = Logger(subsystem: subsystem, category: "notify")
    static let refresh = Logger(subsystem: subsystem, category: "refresh")
}
