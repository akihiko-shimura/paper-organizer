import Foundation
import Vision
import AppKit
// usage: vocr <image> [lang,lang,...]   default: ja-JP,en-US
let path = CommandLine.arguments[1]
let langs = CommandLine.arguments.count > 2
    ? CommandLine.arguments[2].split(separator: ",").map(String.init)
    : ["ja-JP", "en-US"]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(1) }
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = true
req.recognitionLanguages = langs
try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
for o in (req.results ?? []) { if let t = o.topCandidates(1).first { print(t.string) } }
