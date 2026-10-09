# InkSight B2 Is icon

The owner selected this exact B2 design on 2026-10-08: uppercase I followed by lowercase s. The approved 1024px master is preserved byte-for-byte; SHA-256: `1498ad34fc0c6fb89c79c1e1c31c5420bf9c137661da1a9704c4cd970dc1b0d2`.

Provenance: original InkSight design generated with OpenAI's built-in image tool, based on the project's earlier B concept, reviewed and selected by the project owner. No stock photograph, downloaded icon library, or third-party font file is included. Distributed within the project's GPL-3.0-only scope; this provenance statement is not a warranty of copyrightability, exclusivity, or trademark clearance.

`BuildIcons.swift` deterministically resamples the original into a macOS iconset and uses the system iconutil to create AppIcon.icns. It extracts the actual Is lettering from a fixed interior region into a monochrome alpha mask for the menu bar. The application icon is not redrawn and the lettermark is not replaced with another font.

The menu uses an 18-point template image and follows the system's light/dark rendering. On Retina, 18 logical points occupy 36 physical pixels. The 128px template is a high-resolution source, not a claim that the menu itself is 128px. Check 16/18/22 points at both 1x and 2x. Full-color app icons include standard 16/32/128/256/512-point representations and their 2x counterparts.

The release privacy scanner permits only this exact PNG path with the pinned hash. Other PNGs, fonts, firmware binaries, private configuration and runtime state remain excluded. Icons must be regenerated before signing the application bundle; never modify a signed installed application in place.
