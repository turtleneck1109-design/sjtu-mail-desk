import AppKit
import Foundation
let destination = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
for (name, pixels) in [("icon_16x16",16),("icon_16x16@2x",32),("icon_32x32",32),("icon_32x32@2x",64),("icon_128x128",128),("icon_128x128@2x",256),("icon_256x256",256),("icon_256x256@2x",512),("icon_512x512",512),("icon_512x512@2x",1024)] {
    let size = CGFloat(pixels)
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let background = NSBezierPath(roundedRect: NSRect(x: size*0.06, y: size*0.06, width: size*0.88, height: size*0.88), xRadius: size*0.2, yRadius: size*0.2)
    NSColor(red: 0.35, green: 0.29, blue: 0.84, alpha: 1).setFill()
    background.fill()
    let envelope = NSBezierPath(roundedRect: NSRect(x: size*0.22, y: size*0.30, width: size*0.56, height: size*0.40), xRadius: size*0.045, yRadius: size*0.045)
    NSColor.white.setFill(); envelope.fill()
    let fold = NSBezierPath()
    fold.move(to: NSPoint(x:size*0.24,y:size*0.66));fold.line(to:NSPoint(x:size*0.50,y:size*0.47));fold.line(to:NSPoint(x:size*0.76,y:size*0.66))
    fold.lineWidth = size*0.025; fold.lineCapStyle = .round; fold.lineJoinStyle = .round
    NSColor(red: 0.35, green: 0.29, blue: 0.84, alpha: 1).setStroke(); fold.stroke()
    NSGraphicsContext.restoreGraphicsState()
    let data = bitmap.representation(using: .png, properties: [:])!
    try data.write(to: destination.appendingPathComponent(name+".png"))
}
