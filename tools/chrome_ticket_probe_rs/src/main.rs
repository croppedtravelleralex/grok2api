//! Unified Chrome ticket / web pool probes with single SSH snapshot.

mod probes;
mod snapshot;

use anyhow::{bail, Context, Result};
use chrono::Utc;
use clap::{Parser, Subcommand};
use probes::{full_probe_report, ProbeTargets};
use serde_json::Value;
use snapshot::{deploy_snapshot_script, fetch_unified_snapshot};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

#[derive(Parser, Debug)]
#[command(about = "Chrome ticket pool probes (Rust, unified IO)")]
struct Cli {
    #[arg(long, env = "PANDA_SSH", default_value = "panda")]
    ssh_host: String,

    #[arg(long, env = "CHROME_TICKET_QUOTA_WORKERS", default_value_t = 12)]
    workers: u32,

    #[arg(long, default_value = ".tmp/chrome-ticket-probe.jsonl")]
    jsonl: PathBuf,

    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand, Debug, Clone)]
enum Commands {
    /// Full probe report (dispatch + normal + ticket-quota + readiness)
    Probe {
        #[arg(short = 'c', long, default_value_t = 5)]
        concurrent: u32,
        #[arg(short = 'n', long, default_value_t = 20)]
        tickets: u32,
        #[arg(long)]
        quiet: bool,
    },
    /// Ticket ↔ quota audit only
    Audit,
    /// Periodic probe + JSONL append
    Daemon {
        #[arg(long, default_value_t = 300)]
        interval: u64,
        #[arg(short = 'c', long, default_value_t = 5)]
        concurrent: u32,
        #[arg(short = 'n', long, default_value_t = 20)]
        tickets: u32,
        #[arg(long)]
        once: bool,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    deploy_snapshot_script(&cli.ssh_host)?;

    let command = cli.command.clone().unwrap_or(Commands::Probe {
        concurrent: 5,
        tickets: 20,
        quiet: false,
    });

    match command {
        Commands::Probe {
            concurrent,
            tickets,
            quiet,
        } => run_probe(&cli, concurrent, tickets, quiet),
        Commands::Audit => run_audit(&cli),
        Commands::Daemon {
            interval,
            concurrent,
            tickets,
            once,
        } => run_daemon(&cli, interval, concurrent, tickets, once),
    }
}

fn run_probe(cli: &Cli, concurrent: u32, tickets: u32, quiet: bool) -> Result<()> {
    eprintln!(
        "[probe-rs] fetching unified snapshot workers={}…",
        cli.workers
    );
    let snap = fetch_unified_snapshot(&cli.ssh_host, cli.workers)?;
    let report = full_probe_report(
        &snap,
        ProbeTargets {
            concurrent,
            tickets,
        },
    );
    let ready = report
        .get("readiness")
        .and_then(|v| v.get("ready_for_both"))
        .and_then(Value::as_bool)
        .unwrap_or(false);

    if quiet {
        let summary = serde_json::json!({
            "ready_for_both": ready,
            "blockers": report.get("readiness").and_then(|r| r.get("blockers")).cloned().unwrap_or(Value::Null),
            "available_total": report.pointer("/ticket_pool/available_total"),
        });
        println!("{}", serde_json::to_string(&summary)?);
    } else {
        println!("{}", serde_json::to_string_pretty(&report)?);
    }

    std::process::exit(if ready { 0 } else { 1 });
}

fn run_audit(cli: &Cli) -> Result<()> {
    let snap = fetch_unified_snapshot(&cli.ssh_host, cli.workers)?;
    let audit = probes::ticket_quota_audit(&snap, true);
    let violations: Vec<_> = audit.iter().filter(|r| !r.ok).collect();
    let out = serde_json::json!({
        "probe": "ticket_quota_audit",
        "ts": Utc::now().to_rfc3339(),
        "violations_count": violations.len(),
        "all_ok": violations.is_empty(),
        "rows": audit,
        "violations": violations,
    });
    println!("{}", serde_json::to_string_pretty(&out)?);
    if violations.is_empty() {
        Ok(())
    } else {
        bail!("quota violations: {}", violations.len());
    }
}

fn run_daemon(cli: &Cli, interval: u64, concurrent: u32, tickets: u32, once: bool) -> Result<()> {
    loop {
        let snap = fetch_unified_snapshot(&cli.ssh_host, cli.workers)
            .context("daemon snapshot failed")?;
        let report = full_probe_report(
            &snap,
            ProbeTargets {
                concurrent,
                tickets,
            },
        );
        append_jsonl(&cli.jsonl, &report)?;
        let ready = report
            .pointer("/readiness/ready_for_both")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        eprintln!(
            "[probe-rs daemon] ready={} available={:?} blockers={:?}",
            ready,
            report.pointer("/ticket_pool/available_total"),
            report.pointer("/readiness/blockers")
        );
        if once {
            break;
        }
        thread::sleep(Duration::from_secs(interval));
    }
    Ok(())
}

fn append_jsonl(path: &PathBuf, value: &Value) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    writeln!(file, "{}", serde_json::to_string(value)?)?;
    Ok(())
}
