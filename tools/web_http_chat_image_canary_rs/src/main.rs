//! Concurrent rewrite of frozen `web_http_chat_image_canary.v1.py`.
//!
//! - Sign+verify: Playwright via `web_http_sign_helper.py` (frozen v1)
//!   Tickets are path-sensitive / short-lived — helper verifies immediately
//!   (chat or Lite) so a bad `/rest/products` capture is retried.
//! - Extra HTTP: curl_cffi via `web_http_post_worker.py` for contrast A (no-sig).
//! - Concurrency: `--jobs N` accounts in parallel; within an account, A runs
//!   while we do not hold Playwright (after chat verify returns).
//!
//! Native wreq/btls is not used on this Windows host.

use anyhow::{anyhow, bail, Context, Result};
use clap::Parser;
use futures::future::join_all;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Instant;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

const PROXY: &str = "http://127.0.0.1:7897";

#[derive(Parser, Debug)]
#[command(about = "Rust concurrent canary (frozen v1 sign+verify + curl_cffi A)")]
struct Args {
    #[arg(long, default_value = ".tmp/web-sso-canary.json")]
    sso_file: PathBuf,
    #[arg(long)]
    account_id: Option<i64>,
    #[arg(long, default_value_t = 1, help = "parallel accounts")]
    jobs: usize,
    #[arg(long, default_value = ".tmp/web-http-chat-image-canary-rs-result.json")]
    out: PathBuf,
    #[arg(long, default_value = "python")]
    python: String,
    #[arg(long)]
    skip_a: bool,
}

#[derive(Debug, Deserialize)]
struct SsoFile {
    accounts: Vec<Account>,
}

#[derive(Debug, Deserialize, Clone)]
struct Account {
    id: i64,
    sso: String,
}

#[derive(Debug, Deserialize)]
struct SignDump {
    #[serde(rename = "statsigId")]
    statsig_id: Option<String>,
    cookie: Option<String>,
    source: Option<String>,
    error: Option<String>,
    statsig_len: Option<usize>,
    verify: Option<Value>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct StepResult {
    http: u16,
    cf: Option<String>,
    elapsed_s: f64,
    has_statsig: bool,
    statsig_len: usize,
    kind: String,
    has_token_hint: bool,
    image_count: usize,
    image_urls_prefix: Vec<String>,
    body_prefix: String,
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn cookie_from_sso(sso: &str) -> String {
    let mut token = sso.trim().to_string();
    if token.to_ascii_lowercase().starts_with("sso=") {
        token = token[4..].trim().to_string();
    }
    if let Some((head, _)) = token.split_once(';') {
        token = head.trim().to_string();
    }
    format!("sso={token}; sso-rw={token}")
}

fn chat_payload(message: &str, enable_image: bool) -> Value {
    json!({
        "collectionIds": [],
        "disabledConnectorIds": [],
        "deviceEnvInfo": {
            "darkModeEnabled": false,
            "devicePixelRatio": 2,
            "screenHeight": 1328,
            "screenWidth": 2056,
            "viewportHeight": 1083,
            "viewportWidth": 2056
        },
        "disableMemory": true,
        "disableSearch": false,
        "disableSelfHarmShortCircuit": false,
        "disableTextFollowUps": false,
        "enableImageGeneration": enable_image,
        "enableImageStreaming": enable_image,
        "enableSideBySide": true,
        "fileAttachments": [],
        "forceConcise": false,
        "forceSideBySide": false,
        "imageAttachments": [],
        "imageGenerationCount": if enable_image { 2 } else { 0 },
        "isAsyncChat": false,
        "message": message,
        "modeId": "fast",
        "responseMetadata": {},
        "returnImageBytes": false,
        "returnRawGrokInXaiRequest": false,
        "sendFinalMetadata": true,
        "temporary": true
    })
}

fn load_accounts(path: &Path, account_id: Option<i64>, limit: usize) -> Result<Vec<Account>> {
    let raw = std::fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
    let file: SsoFile = serde_json::from_str(&raw).context("parse sso file")?;
    let mut accounts: Vec<_> = file.accounts.into_iter().filter(|a| !a.sso.is_empty()).collect();
    if let Some(id) = account_id {
        accounts.retain(|a| a.id == id);
    }
    accounts.truncate(limit.max(1));
    if accounts.is_empty() {
        bail!("no accounts");
    }
    Ok(accounts)
}

async fn run_sign_helper(
    python: &str,
    sso_file: &Path,
    account_id: i64,
    out: &Path,
    mode: &str,
) -> Result<SignDump> {
    let helper = repo_root().join("tools").join("web_http_sign_helper.py");
    let mut cmd = Command::new(python);
    cmd.arg(&helper)
        .arg("--sso-file")
        .arg(sso_file)
        .arg("--account-id")
        .arg(account_id.to_string())
        .arg("--out")
        .arg(out)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .current_dir(repo_root());
    match mode {
        "image" => {
            cmd.arg("--verify-image").arg("--no-verify-chat");
        }
        "chat" => { /* default verify chat */ }
        _ => {
            cmd.arg("--no-verify-chat");
        }
    }
    let output = cmd.output().await.context("spawn sign helper")?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    for line in stdout.lines() {
        if line.starts_with('{') {
            println!("{line}");
        }
    }
    if !output.status.success() {
        bail!(
            "sign helper failed status={:?} stderr={}",
            output.status.code(),
            stderr.chars().take(400).collect::<String>()
        );
    }
    let raw = std::fs::read_to_string(out).context("read sign dump")?;
    serde_json::from_str(&raw).context("parse sign dump")
}

async fn post_worker(
    python: &str,
    cookie: &str,
    statsig: Option<&str>,
    payload: Value,
    want_image: bool,
) -> Result<StepResult> {
    let worker = repo_root().join("tools").join("web_http_post_worker.py");
    let req = json!({
        "cookie": cookie,
        "statsig": statsig,
        "payload": payload,
        "want_image": want_image
    });
    let mut child = Command::new(python)
        .arg(&worker)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .current_dir(repo_root())
        .spawn()
        .context("spawn post worker")?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(serde_json::to_string(&req)?.as_bytes())
            .await
            .context("write worker stdin")?;
    }
    let output = child.wait_with_output().await.context("wait worker")?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        bail!(
            "post worker failed status={:?} stderr={}",
            output.status.code(),
            stderr.chars().take(300).collect::<String>()
        );
    }
    let line = stdout
        .lines()
        .rev()
        .find(|l| l.starts_with('{'))
        .ok_or_else(|| anyhow!("no json from worker: {stdout}"))?;
    serde_json::from_str(line).context("parse worker json")
}

fn step_from_verify(verify: &Value, statsig_len: usize, image: bool) -> Option<StepResult> {
    let kind = verify.get("kind")?.as_str()?;
    let ok = if image {
        kind == "image_ok" || verify.get("image_count").and_then(|v| v.as_u64()).unwrap_or(0) > 0
    } else {
        kind == "chat_ok"
    };
    if !ok {
        return None;
    }
    let urls = verify
        .get("image_urls_prefix")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(|s| s.to_string()))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    Some(StepResult {
        http: verify.get("http").and_then(|v| v.as_u64()).unwrap_or(200) as u16,
        cf: None,
        elapsed_s: 0.0,
        has_statsig: true,
        statsig_len,
        kind: if image { "image_ok".into() } else { "chat_ok".into() },
        has_token_hint: !image,
        image_count: verify
            .get("image_count")
            .and_then(|v| v.as_u64())
            .unwrap_or(urls.len() as u64) as usize,
        image_urls_prefix: urls,
        body_prefix: "verified_in_sign_helper".into(),
    })
}

async fn run_one_account(python: &str, sso_file: &Path, account: Account, skip_a: bool) -> Result<Value> {
    let sso_cookie = cookie_from_sso(&account.sso);
    let wall = Instant::now();
    let sign_out = repo_root()
        .join(".tmp")
        .join(format!("web-http-sign-dump-{}.json", account.id));

    // Launch A (no-sig) in parallel with B+C (sign+verify chat).
    let a_fut = async {
        if skip_a {
            return Ok::<Option<StepResult>, anyhow::Error>(None);
        }
        let a = post_worker(
            python,
            &sso_cookie,
            None,
            chat_payload("Reply with exactly: PONG", false),
            false,
        )
        .await?;
        println!(
            "{}",
            json!({"event":"A_no_sig","account_id":account.id,"http":a.http,"kind":a.kind,"elapsed_s":a.elapsed_s})
        );
        Ok(Some(a))
    };

    let bc_fut = async {
        let t_sign = Instant::now();
        let sign = run_sign_helper(python, sso_file, account.id, &sign_out, "chat").await?;
        let statsig = sign
            .statsig_id
            .clone()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| anyhow!("account {}: no statsig: {:?}", account.id, sign.error))?;
        let verify = sign.verify.clone().unwrap_or(json!({}));
        println!(
            "{}",
            json!({
                "event":"B_sign",
                "account_id": account.id,
                "source": sign.source,
                "has_statsig": true,
                "statsig_len": sign.statsig_len.unwrap_or(statsig.len()),
                "error": sign.error,
                "verify": verify,
                "sign_wall_s": (t_sign.elapsed().as_secs_f64()*100.0).round()/100.0
            })
        );
        let c = step_from_verify(&verify, statsig.len(), false).ok_or_else(|| {
            anyhow!(
                "account {}: chat verify failed: {}",
                account.id,
                verify
            )
        })?;
        println!(
            "{}",
            json!({"event":"C_signed_chat","account_id":account.id,"http":c.http,"kind":c.kind,"has_token_hint":c.has_token_hint,"elapsed_s":c.elapsed_s})
        );
        Ok::<_, anyhow::Error>((sign, statsig, c))
    };

    let (a_res, bc_res) = tokio::join!(a_fut, bc_fut);
    let a_step = a_res?;
    let (sign, statsig, c) = bc_res?;

    // B2+D: fresh sign + Lite verify (must be after chat — Playwright serial on Windows).
    let sign2_out = repo_root()
        .join(".tmp")
        .join(format!("web-http-sign-dump-image-{}.json", account.id));
    let t_sign2 = Instant::now();
    let sign2 = run_sign_helper(python, sso_file, account.id, &sign2_out, "image").await?;
    let statsig2 = sign2
        .statsig_id
        .clone()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| anyhow!("account {}: no image statsig: {:?}", account.id, sign2.error))?;
    let verify2 = sign2.verify.clone().unwrap_or(json!({}));
    println!(
        "{}",
        json!({
            "event":"B2_sign_image",
            "account_id": account.id,
            "source": sign2.source,
            "has_statsig": true,
            "statsig_len": sign2.statsig_len.unwrap_or(statsig2.len()),
            "verify": verify2,
            "sign_wall_s": (t_sign2.elapsed().as_secs_f64()*100.0).round()/100.0
        })
    );
    let d = step_from_verify(&verify2, statsig2.len(), true).ok_or_else(|| {
        anyhow!(
            "account {}: image verify failed: {}",
            account.id,
            verify2
        )
    })?;
    println!(
        "{}",
        json!({"event":"D_lite_image","account_id":account.id,"http":d.http,"kind":d.kind,"image_count":d.image_count,"image_urls_prefix":d.image_urls_prefix,"elapsed_s":d.elapsed_s})
    );

    let a_ok = a_step
        .as_ref()
        .map(|a| a.kind == "anti_bot_403")
        .unwrap_or(true);
    let c_ok = c.kind == "chat_ok";
    let d_ok = d.kind == "image_ok" || d.image_count > 0;
    Ok(json!({
        "account_id": account.id,
        "impl": "rust-orch+curl_cffi",
        "A_no_sig": a_step,
        "B_sign": {
            "source": sign.source,
            "has_statsig": true,
            "statsig_len": statsig.len(),
            "error": sign.error
        },
        "C_signed_chat": c,
        "D_lite_image": d,
        "acceptance": {
            "A_anti_bot": a_ok,
            "C_chat_ok": c_ok,
            "D_image_ok": d_ok
        },
        "wall_s": (wall.elapsed().as_secs_f64()*100.0).round()/100.0
    }))
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let root = repo_root();
    let sso_file = if args.sso_file.is_absolute() {
        args.sso_file.clone()
    } else {
        root.join(&args.sso_file)
    };
    let out_path = if args.out.is_absolute() {
        args.out.clone()
    } else {
        root.join(&args.out)
    };
    let accounts = load_accounts(&sso_file, args.account_id, args.jobs)?;
    println!(
        "{}",
        json!({
            "event":"start",
            "accounts": accounts.iter().map(|a| a.id).collect::<Vec<_>>(),
            "proxy": PROXY,
            "impl": "rust-orch+curl_cffi",
            "jobs": accounts.len(),
            "frozen_v1": "tools/web_http_chat_image_canary.v1.py"
        })
    );

    let python = args.python.clone();
    let skip_a = args.skip_a;
    let futs: Vec<_> = accounts
        .into_iter()
        .map(|acc| {
            let python = python.clone();
            let sso_file = sso_file.clone();
            async move { run_one_account(&python, &sso_file, acc, skip_a).await }
        })
        .collect();
    let results = join_all(futs).await;

    let mut rows = Vec::new();
    let mut all_ok = true;
    for r in results {
        let row = r?;
        let ok = row["acceptance"]["A_anti_bot"].as_bool().unwrap_or(false)
            && row["acceptance"]["C_chat_ok"].as_bool().unwrap_or(false)
            && row["acceptance"]["D_image_ok"].as_bool().unwrap_or(false);
        all_ok &= ok;
        println!(
            "{}",
            json!({
                "event":"acceptance",
                "account_id": row["account_id"],
                "A_anti_bot": row["acceptance"]["A_anti_bot"],
                "C_chat_ok": row["acceptance"]["C_chat_ok"],
                "D_image_ok": row["acceptance"]["D_image_ok"],
                "wall_s": row["wall_s"]
            })
        );
        rows.push(row);
    }

    if let Some(parent) = out_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(
        &out_path,
        serde_json::to_string_pretty(&json!({"impl":"rust-orch+curl_cffi","results": rows}))?,
    )?;
    println!(
        "{}",
        json!({"event":"wrote","path": out_path.display().to_string(), "all_ok": all_ok})
    );
    if !all_ok {
        std::process::exit(2);
    }
    Ok(())
}
