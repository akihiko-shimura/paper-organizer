// PDF の全頁の本文を標準出力へ。頁の終わりに改頁(U+000C)を付ける(pdftotext と同じ形)。
// pdftotext が時間切れ・失敗したときの代わりに organize.py が呼ぶ(papers fulltext)。
// 例: InDesign が書き出した図の多い頁で、Poppler が色の定義を図形ごとに読み直し 1 頁に 97 秒かかる PDF が、これなら 0.1 秒。
import Foundation
import PDFKit

guard CommandLine.arguments.count == 2,
      let doc = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])), !doc.isLocked else { exit(1) }
var out = ""
for i in 0..<doc.pageCount {
    out += (doc.page(at: i)?.string ?? "") + "\u{0C}"
}
FileHandle.standardOutput.write(out.data(using: .utf8) ?? Data())
