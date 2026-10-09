import CoreGraphics

public struct ScreenGeometry: Sendable {
    public let frame: CGRect
    public let visibleFrame: CGRect

    public init(frame: CGRect, visibleFrame: CGRect) {
        self.frame = frame
        self.visibleFrame = visibleFrame
    }
}

public enum PanelPlacement {
    public static func frame(
        panelSize: CGSize,
        screens: [ScreenGeometry],
        mouseLocation: CGPoint,
        margin: CGFloat = 16
    ) -> CGRect? {
        guard let target = screens.first(where: { $0.frame.contains(mouseLocation) }) ?? screens.first else {
            return nil
        }

        let visible = target.visibleFrame
        let desiredOrigin = CGPoint(
            x: visible.midX - panelSize.width / 2,
            y: visible.minY + visible.height * 0.68 - panelSize.height / 2
        )

        let minX = visible.minX + margin
        let maxX = visible.maxX - panelSize.width - margin
        let minY = visible.minY + margin
        let maxY = visible.maxY - panelSize.height - margin

        let origin = CGPoint(
            x: clamp(desiredOrigin.x, minimum: minX, maximum: maxX),
            y: clamp(desiredOrigin.y, minimum: minY, maximum: maxY)
        )

        return CGRect(origin: origin, size: panelSize)
    }

    private static func clamp(_ value: CGFloat, minimum: CGFloat, maximum: CGFloat) -> CGFloat {
        guard minimum <= maximum else {
            return (minimum + maximum) / 2
        }
        return min(max(value, minimum), maximum)
    }
}
