use anyhow::{Context, Result};
use serde_json::Value;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const SNAPSHOT_SCRIPT: &str = "panda_unified_pool_snapshot.py";

pub fn repo_tools_dir() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("tools parent")
        .to_path_buf()
}

pub fn deploy_snapshot_script(ssh_host: &str) -> Result<()> {
    let local = repo_tools_dir().join(SNAPSHOT_SCRIPT);
    anyhow::ensure!(local.is_file(), "missing {}", local.display());
    let status = Command::new("scp")
        .args([
            local.to_str().context("path utf8")?,
            &format!("{ssh_host}:/tmp/{SNAPSHOT_SCRIPT}"),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .status()
        .context("scp snapshot script")?;
    anyhow::ensure!(status.success(), "scp failed: {:?}", status);
    Ok(())
}

pub fn fetch_unified_snapshot(ssh_host: &str, workers: u32) -> Result<Value> {
    let started = Instant::now();
    let remote = format!(
        "python3 /tmp/{SNAPSHOT_SCRIPT} --workers {workers}"
    );
    let output = Command::new("ssh")
        .args([ssh_host, &remote])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .context("ssh unified snapshot")?;
    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr);
        let out = String::from_utf8_lossy(&output.stdout);
        anyhow::bail!(
            "snapshot failed ({}s): {} {}",
            started.elapsed().as_secs_f32(),
            err.trim(),
            out.chars().take(200).collect::<String>()
        );
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let line = text
        .lines()
        .filter(|l| l.trim_start().starts_with('{'))
        .last()
        .context("no json line in snapshot output")?;
    let mut value: Value = serde_json::from_str(line).context("parse snapshot json")?;
    if let Some(obj) = value.as_object_mut() {
        obj.insert(
            "fetch_elapsed_s".into(),
            Value::from(started.elapsed().as_secs_f32()),
        );
    }
    eprintln!(
        "[probe-rs] snapshot ok in {:.1}s",
        started.elapsed().as_secs_f32()
    );
    Ok(value)
}

#[allow(dead_code)]
pub fn ssh_run(ssh_host: &str, command: &str, timeout: Duration) -> Result<String> {
    let output = Command::new("ssh")
        .args([ssh_host, command])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .context("ssh")?;
    let _ = timeout; // best-effort; std::process has no timeout without extra crates
    if !output.status.success() {
        anyhow::bail!(
            "ssh failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}
