import AppKit

/// Deterministic asset conversion only. The approved master is never redrawn.
@main struct BuildIcons {
    static func bitmap(_ w: Int, _ h: Int) -> NSBitmapImageRep {
        let value = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        value.size = NSSize(width: w, height: h)
        return value
    }
    static func write(_ image: NSImage, pixels: Int, to url: URL) throws {
        let value = bitmap(pixels, pixels)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: value)
        NSGraphicsContext.current?.imageInterpolation = .high
        image.draw(in: NSRect(x: 0, y: 0, width: pixels, height: pixels))
        NSGraphicsContext.restoreGraphicsState()
        try value.representation(using: .png, properties: [:])!.write(to: url)
    }
    static func main() throws {
        let source = URL(fileURLWithPath: CommandLine.arguments[1])
        let output = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        let data = try Data(contentsOf: source)
        guard let master = NSImage(data: data), let pixels = NSBitmapImageRep(data: data),
              pixels.pixelsWide == 1024, pixels.pixelsHigh == 1024 else {
            throw NSError(domain: "InkSightIcon", code: 1)
        }
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let scratch = FileManager.default.temporaryDirectory.appendingPathComponent("InkSightIcons-" + UUID().uuidString)
        let iconset = scratch.appendingPathComponent("AppIcon.iconset")
        try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: scratch) }
        for size in [16, 32, 128, 256, 512] {
            for scale in [1, 2] {
                let suffix = scale == 2 ? "@2x" : ""
                try write(master, pixels: size * scale,
                    to: iconset.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
            }
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
        process.arguments = ["-c", "icns", iconset.path, "-o", output.appendingPathComponent("AppIcon.icns").path]
        try process.run(); process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw NSError(domain: "InkSightIcon", code: 2) }

        // This ROI contains only the approved Is glyphs and warm-white screen.
        // Extracting their luminance mask preserves exact letter shapes; no font is used.
        let glyph = bitmap(420, 340)
        for y in 0..<340 {
            for x in 0..<420 {
                guard let color = pixels.colorAt(x: 300 + x, y: 340 + y)?.usingColorSpace(.deviceRGB) else { continue }
                let luminance = 0.2126 * color.redComponent + 0.7152 * color.greenComponent + 0.0722 * color.blueComponent
                let alpha = min(1, max(0, (0.72 - luminance) / 0.5))
                // NSBitmapImageRep.setColor requires matching RGB components;
                // a grayscale NSColor silently writes zero alpha on macOS 26.
                glyph.setColor(NSColor(deviceRed: 0, green: 0, blue: 0, alpha: alpha), atX: x, y: y)
            }
        }
        let glyphImage = NSImage(size: glyph.size); glyphImage.addRepresentation(glyph)
        let template = bitmap(128, 128)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: template)
        NSGraphicsContext.current?.imageInterpolation = .high
        glyphImage.draw(in: NSRect(x: 6, y: 17, width: 116, height: 94))
        NSGraphicsContext.restoreGraphicsState()
        let opaque = (0..<128).reduce(0) { count, x in
            count + (0..<128).filter { template.colorAt(x: x, y: $0)!.alphaComponent > 0.5 }.count
        }
        guard opaque > 200, opaque < 12000 else {
            throw NSError(domain: "InkSightIconEmptyTemplate", code: 3)
        }
        try template.representation(using: .png, properties: [:])!
            .write(to: output.appendingPathComponent("MenuIconTemplate.png"))
        print("B2 master preserved; icns and same-glyph template generated")
    }
}
