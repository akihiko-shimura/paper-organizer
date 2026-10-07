import Foundation
import NaturalLanguage
// 1行=1文 を読み、512次元の文ベクトルをTSVで返す（英語）
guard let e = NLEmbedding.sentenceEmbedding(for: .english) else { exit(1) }
while let line = readLine() {
    let v = e.vector(for: line) ?? []
    print(v.map { String(format: "%.5f", $0) }.joined(separator: "\t"))
}
