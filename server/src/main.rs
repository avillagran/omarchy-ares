#[tokio::main]
async fn main() {
    let code = match ares_server::cli::run(std::env::args().skip(1).collect()).await {
        Ok(code) => code,
        Err(error) => {
            ares_server::cli::print(serde_json::json!({"ok":false,"error":error.to_string()}));
            1
        }
    };
    std::process::exit(code);
}
