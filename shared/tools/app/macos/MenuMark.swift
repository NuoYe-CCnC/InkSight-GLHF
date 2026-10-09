import AppKit

/// Mask extracted from the exact user-approved B2 Is lettering. No font substitution.
enum MenuMark {
    static func image(size: CGFloat = 18) -> NSImage? {
        guard let url = Bundle.main.url(forResource: "MenuIconTemplate", withExtension: "png"),
              let image = NSImage(contentsOf: url) else { return nil }
        image.size = NSSize(width: size, height: size)
        image.isTemplate = true
        return image
    }
}
