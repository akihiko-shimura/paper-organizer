// 検索の GUI の窓。Python のサーバ(gui.py)を子プロセスで起動し、その頁を窓に読ませる。検索の中身は持たない。
// 設計は GUI_DESIGN.md「構成」「守り」。画面には Tauri の機能(IPC)の権限を何も与えない(capabilities を置かない)。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

struct Server(Mutex<Option<Child>>);

fn token() -> String {
    let mut b = [0u8; 32];
    getrandom::fill(&mut b).expect("no source of random bytes");
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

fn main() {
    // 番号は OS に選ばせる。合言葉は環境変数で渡す(引数に書くと、ほかの利用者が ps で読める)
    let port = TcpListener::bind("127.0.0.1:0").and_then(|l| l.local_addr()).expect("no free port").port();
    let token = token();
    let gui = std::env::var_os("PAPERS_GUI")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../gui.py"));
    let python = std::env::var("PAPERS_PYTHON").unwrap_or_else(|_| "python3".into());
    // Finder から開いたアプリの PATH には Homebrew の置き場が無く、頁の絵を作る pdftoppm が見つからない。後ろに足す
    // (前には足さない: python3 は、端末からでも Finder からでも PATH の先頭側のものを使う)
    let path = std::env::var("PATH").unwrap_or_default();
    let path = std::iter::once(path.as_str())
        .chain(["/opt/homebrew/bin", "/usr/local/bin"])
        .filter(|p| !p.is_empty())
        .collect::<Vec<_>>()
        .join(":");
    let mut child = Command::new(&python)
        .arg(&gui)
        .arg("--parent-watch")
        .arg(format!("--port={}", port))
        .env("PAPERS_GUI_TOKEN", &token)
        .env("PATH", &path)
        .stdin(Stdio::piped()) // つないだままにする。この過程が消えると閉じて、サーバが終わる
        .spawn()
        .unwrap_or_else(|e| panic!("cannot start {} {}: {}", python, gui.display(), e));
    for _ in 0..100 {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            break;
        }
        if let Ok(Some(status)) = child.try_wait() {
            panic!("the server ({} {}) stopped at startup: {}", python, gui.display(), status);
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let url = format!("http://127.0.0.1:{}/#t={}", port, token);
    // 検査用: 窓を見せずに立ち上げ、数秒で終わる(頁が読まれたことは、サーバの記録で見る)
    let smoke = std::env::var_os("PAPERS_APP_SMOKE").is_some();
    let app = tauri::Builder::default()
        .manage(Server(Mutex::new(Some(child))))
        .setup(move |app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.parse().unwrap()))
                .title("papers")
                .inner_size(1200.0, 800.0)
                .visible(!smoke)
                .build()?;
            if smoke {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(6));
                    handle.exit(0);
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("cannot open the window");
    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut c) = handle.state::<Server>().0.lock().unwrap().take() {
                let _ = c.kill();
                let _ = c.wait();
            }
        }
    });
}
