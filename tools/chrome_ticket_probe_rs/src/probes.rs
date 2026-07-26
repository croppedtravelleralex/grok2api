use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Map, Value};

#[derive(Clone, Copy)]
pub struct ProbeTargets {
    pub concurrent: u32,
    pub tickets: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct AuditRow {
    pub account_id: u64,
    pub available_tickets: u32,
    pub imagine_remaining: u32,
    pub over_by: u32,
    pub ok: bool,
    pub in_dispatch: bool,
    pub in_pin: bool,
    pub in_runtime: bool,
    pub orphan_ticket: bool,
}

pub fn full_probe_report(snap: &Value, targets: ProbeTargets) -> Value {
    let dispatch_probe = dispatch_pool_probe(snap);
    let normal_probe = normal_pool_probe(snap);
    let audit_rows = ticket_quota_audit(snap, true);
    let ticket_probe = ticket_pool_probe(snap, &audit_rows, targets);
    let readiness = readiness_probe(snap, &audit_rows, targets);

    json!({
        "probe": "chrome_ticket_unified",
        "engine": "rust",
        "ts": Utc::now().to_rfc3339(),
        "fetch_elapsed_s": snap.get("fetch_elapsed_s"),
        "targets": {
            "concurrent": targets.concurrent,
            "tickets": targets.tickets,
        },
        "dispatch_pool": dispatch_probe,
        "normal_pool": normal_probe,
        "ticket_pool": ticket_probe,
        "ticket_quota_audit": {
            "rows": audit_rows.clone(),
            "violations": audit_rows.iter().filter(|r| !r.ok).cloned().collect::<Vec<_>>(),
            "violation_count": audit_rows.iter().filter(|r| !r.ok).count(),
        },
        "readiness": readiness,
        "pool_diff": snap.get("pool_diff"),
        "lane_quota": snap.get("lane_quota"),
        "meta": snap.get("meta"),
    })
}

pub fn dispatch_pool_probe(snap: &Value) -> Value {
    let pools = snap.pointer("/web_pools").unwrap_or(&Value::Null);
    let dispatch_ids = ids_from_pool_field(pools, "imageDispatchPoolIds");
    let runtime_ids = ids_from_pool_field(pools, "imagePoolIds");
    let pin_ids = ids_from_pool_field(pools, "imagePinIds");
    let four = pools.pointer("/fourPools/image").unwrap_or(&Value::Null);
    json!({
        "dispatch_count": dispatch_ids.len(),
        "dispatch_ids_head": head_ids(&dispatch_ids, 20),
        "runtime_count": runtime_ids.len(),
        "runtime_ids": runtime_ids,
        "pin_count": pin_ids.len(),
        "pin_ids": pin_ids,
        "pin_not_in_dispatch": pools.get("pinNotInDispatch").cloned().unwrap_or(Value::Array(vec![])),
        "selection_diagnostics": pools.get("selectionDiagnostics"),
        "four_pools_image": four,
        "schedulable_remaining": snap.pointer("/lane_quota/imageSchedulableRemaining"),
        "schedulable_accounts": snap.pointer("/lane_quota/imageSchedulableAccounts"),
    })
}

pub fn normal_pool_probe(snap: &Value) -> Value {
    let pools = snap.pointer("/web_pools").unwrap_or(&Value::Null);
    let four = pools.pointer("/fourPools/image").unwrap_or(&Value::Null);
    let schedulable = ids_from_pool_field(pools, "imageSchedulableIds");
    let dispatch = ids_from_pool_field(pools, "imageDispatchPoolIds");
    let diff = snap.get("pool_diff").cloned().unwrap_or(Value::Null);
    json!({
        "four_pools_image": four,
        "normal_count": four.get("normal").and_then(Value::as_u64).unwrap_or(0),
        "verification_count": four.get("verification").and_then(Value::as_u64).unwrap_or(0),
        "delete_count": four.get("delete").and_then(Value::as_u64).unwrap_or(0),
        "schedulable_count": schedulable.len(),
        "dispatch_count": dispatch.len(),
        "pool_diff": diff,
        "note": "normal/verification maintained by WebMaintenanceProbeTick; no ticket mint",
    })
}

pub fn ticket_quota_audit(snap: &Value, include_dispatch_without_tickets: bool) -> Vec<AuditRow> {
    let pools = snap.pointer("/web_pools").unwrap_or(&Value::Null);
    let stats = snap.pointer("/pool_stats").unwrap_or(&Value::Null);
    let imagine = snap
        .get("imagine_by_account")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();

    let dispatch: std::collections::HashSet<u64> =
        ids_from_pool_field(pools, "imageDispatchPoolIds")
            .into_iter()
            .collect();
    let pin: std::collections::HashSet<u64> = ids_from_pool_field(pools, "imagePinIds")
        .into_iter()
        .collect();
    let runtime: std::collections::HashSet<u64> = ids_from_pool_field(pools, "imagePoolIds")
        .into_iter()
        .collect();

    let dispatch_vec = ids_from_pool_field(pools, "imageDispatchPoolIds");
    let mut rows: Vec<AuditRow> = Vec::new();
    let holders = stats
        .get("AvailableByAccount")
        .or_else(|| stats.get("availableByAccount"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    for item in holders {
        let aid = account_id_from_holder(&item);
        if aid == 0 {
            continue;
        }
        let avail = holder_count(&item);
        let quota = imagine_get(&imagine, aid);
        let in_dispatch = dispatch.contains(&aid);
        rows.push(AuditRow {
            account_id: aid,
            available_tickets: avail,
            imagine_remaining: quota,
            over_by: avail.saturating_sub(quota),
            ok: avail <= quota,
            in_dispatch,
            in_pin: pin.contains(&aid),
            in_runtime: runtime.contains(&aid),
            orphan_ticket: avail > 0 && !in_dispatch,
        });
    }

    if include_dispatch_without_tickets {
        for aid in &dispatch_vec {
            if rows.iter().any(|r| r.account_id == *aid) {
                continue;
            }
            let quota = imagine_get(&imagine, *aid);
            rows.push(AuditRow {
                account_id: *aid,
                available_tickets: 0,
                imagine_remaining: quota,
                over_by: 0,
                ok: true,
                in_dispatch: true,
                in_pin: pin.contains(aid),
                in_runtime: runtime.contains(aid),
                orphan_ticket: false,
            });
        }
    }

    rows.sort_by(|a, b| b.over_by.cmp(&a.over_by).then(a.account_id.cmp(&b.account_id)));
    rows
}

fn ticket_pool_probe(snap: &Value, audit_rows: &[AuditRow], targets: ProbeTargets) -> Value {
    let stats = snap.pointer("/pool_stats").unwrap_or(&Value::Null);
    let by_status = stats
        .get("ByStatus")
        .or_else(|| stats.get("byStatus"))
        .cloned()
        .unwrap_or(Value::Null);
    let available_total = by_status
        .get("available")
        .and_then(Value::as_u64)
        .unwrap_or(0) as u32;

    let violations: Vec<AuditRow> = audit_rows
        .iter()
        .filter(|r| !r.ok)
        .cloned()
        .collect();
    let ticket_holders: Vec<AuditRow> = audit_rows
        .iter()
        .filter(|r| r.available_tickets > 0)
        .cloned()
        .collect();
    let schedulable_runtime = ticket_holders
        .iter()
        .filter(|r| r.in_runtime)
        .count();

    let dispatch_ids = ids_from_pool_field(
        snap.pointer("/web_pools").unwrap_or(&Value::Null),
        "imageDispatchPoolIds",
    );
    let imagine: std::collections::HashMap<u64, u32> = audit_rows
        .iter()
        .map(|r| (r.account_id, r.imagine_remaining))
        .collect();
    let work = dispatch_mint_worklist(snap, &dispatch_ids, &imagine, targets.tickets);
    let mint_headroom_total: u32 = work.iter().filter_map(|w| w.get("headroom").and_then(Value::as_u64)).map(|x| x as u32).sum();

    json!({
        "by_status": by_status,
        "available_total": available_total,
        "ticket_holders": ticket_holders,
        "violations": violations,
        "schedulable_runtime_count": schedulable_runtime,
        "mint_pipeline": {
            "worklist_count": work.len(),
            "worklist_head": work.iter().take(15).collect::<Vec<_>>(),
            "mint_headroom_total": mint_headroom_total,
            "tickets_needed": targets.tickets.saturating_sub(available_total),
        },
    })
}

fn readiness_probe(snap: &Value, audit_rows: &[AuditRow], targets: ProbeTargets) -> Value {
    let stats = snap.pointer("/pool_stats").unwrap_or(&Value::Null);
    let pools = snap.pointer("/web_pools").unwrap_or(&Value::Null);
    let available_total = stats
        .pointer("/ByStatus/available")
        .or_else(|| stats.pointer("/byStatus/available"))
        .and_then(Value::as_u64)
        .unwrap_or(0) as u32;

    let violations = audit_rows_violations(audit_rows);
    let ticket_holders: Vec<_> = audit_rows
        .iter()
        .filter(|r| r.available_tickets > 0)
        .collect();
    let schedulable_runtime = ticket_holders.iter().filter(|r| r.in_runtime).count();
    let orphans: Vec<u64> = ticket_holders
        .iter()
        .filter(|r| r.orphan_ticket)
        .map(|r| r.account_id)
        .collect();
    let pin_not_in_dispatch = pools
        .get("pinNotInDispatch")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let dispatch_ids = ids_from_pool_field(pools, "imageDispatchPoolIds");
    let imagine: std::collections::HashMap<u64, u32> = audit_rows
        .iter()
        .map(|r| (r.account_id, r.imagine_remaining))
        .collect();
    let work = dispatch_mint_worklist(snap, &dispatch_ids, &imagine, targets.tickets);
    let mint_headroom_total: u32 = work
        .iter()
        .filter_map(|w| w.get("headroom").and_then(Value::as_u64))
        .map(|x| x as u32)
        .sum();
    let tickets_needed = targets.tickets.saturating_sub(available_total);

    let mut blockers: Vec<String> = Vec::new();
    if available_total < targets.tickets {
        blockers.push(format!(
            "ticket_pool_available={available_total} < target_tickets={}",
            targets.tickets
        ));
    }
    if (schedulable_runtime as u32) < targets.concurrent {
        blockers.push(format!(
            "runtime_ticket_accounts={schedulable_runtime} < target_concurrent={}",
            targets.concurrent
        ));
    }
    if !orphans.is_empty() {
        blockers.push(format!("orphan_tickets_on_non_dispatch={orphans:?}"));
    }
    if !pin_not_in_dispatch.is_empty() {
        blockers.push(format!("pin_not_in_dispatch={pin_not_in_dispatch:?}"));
    }
    if !violations.is_empty() {
        blockers.push(format!("quota_violations={}", violations.len()));
    }
    if mint_headroom_total < tickets_needed {
        blockers.push(format!(
            "mint_headroom_total={mint_headroom_total} < tickets_needed={tickets_needed}"
        ));
    }

    let ready_for_tickets = available_total >= targets.tickets && violations.is_empty();
    let ready_for_concurrent =
        (schedulable_runtime as u32) >= targets.concurrent && violations.is_empty();
    let ready_for_both = ready_for_tickets
        && ready_for_concurrent
        && orphans.is_empty()
        && pin_not_in_dispatch.is_empty();

    json!({
        "ready_for_tickets": ready_for_tickets,
        "ready_for_concurrent": ready_for_concurrent,
        "ready_for_both": ready_for_both,
        "blockers": blockers,
    })
}

fn dispatch_mint_worklist(
    snap: &Value,
    dispatch_ids: &[u64],
    imagine: &std::collections::HashMap<u64, u32>,
    target_tickets: u32,
) -> Vec<Value> {
    let stats = snap.pointer("/pool_stats").unwrap_or(&Value::Null);
    let dispatch_len = dispatch_ids.len().max(1) as u32;
    let base_target = (target_tickets + dispatch_len - 1) / dispatch_len;
    let mut work: Vec<Value> = Vec::new();

    for aid in dispatch_ids {
        let cap = imagine.get(aid).copied().unwrap_or(0);
        if cap == 0 {
            continue;
        }
        let available = pool_available_for_account(stats, *aid);
        let eff_target = base_target.min(cap);
        if available >= cap || available >= eff_target {
            continue;
        }
        let headroom = eff_target.saturating_sub(available);
        if headroom == 0 {
            continue;
        }
        work.push(json!({
            "account_id": aid,
            "headroom": headroom,
            "imagine_cap": cap,
            "pool_available": available,
            "target": eff_target,
        }));
    }
    work.sort_by(|a, b| {
        let ha = a.get("headroom").and_then(Value::as_u64).unwrap_or(0);
        let hb = b.get("headroom").and_then(Value::as_u64).unwrap_or(0);
        hb.cmp(&ha).then_with(|| {
            a.get("account_id")
                .and_then(Value::as_u64)
                .unwrap_or(0)
                .cmp(&b.get("account_id").and_then(Value::as_u64).unwrap_or(0))
        })
    });
    work
}

fn pool_available_for_account(stats: &Value, account_id: u64) -> u32 {
    let holders = stats
        .get("AvailableByAccount")
        .or_else(|| stats.get("availableByAccount"))
        .and_then(Value::as_array);
    let Some(items) = holders else {
        return 0;
    };
    for item in items {
        if account_id_from_holder(item) == account_id {
            return holder_count(item);
        }
    }
    0
}

fn audit_rows_violations(rows: &[AuditRow]) -> Vec<&AuditRow> {
    rows.iter().filter(|r| !r.ok).collect()
}

fn ids_from_pool_field(pools: &Value, field: &str) -> Vec<u64> {
    pools
        .get(field)
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_u64().or_else(|| v.as_i64().map(|x| x as u64)))
                .filter(|&id| id > 0)
                .collect::<Vec<_>>()
        })
        .map(|mut v| {
            v.sort_unstable();
            v.dedup();
            v
        })
        .unwrap_or_default()
}

fn head_ids(ids: &[u64], n: usize) -> Vec<u64> {
    ids.iter().take(n).copied().collect()
}

fn account_id_from_holder(item: &Value) -> u64 {
    item.get("AccountID")
        .or_else(|| item.get("accountId"))
        .or_else(|| item.get("account_id"))
        .and_then(Value::as_u64)
        .or_else(|| {
            item.get("AccountID")
                .or_else(|| item.get("accountId"))
                .or_else(|| item.get("account_id"))
                .and_then(Value::as_i64)
                .map(|x| x as u64)
        })
        .unwrap_or(0)
}

fn holder_count(item: &Value) -> u32 {
    item.get("Count")
        .or_else(|| item.get("count"))
        .and_then(Value::as_u64)
        .or_else(|| {
            item.get("Count")
                .or_else(|| item.get("count"))
                .and_then(Value::as_i64)
                .map(|x| x as u64)
        })
        .unwrap_or(0) as u32
}

fn imagine_get(map: &Map<String, Value>, aid: u64) -> u32 {
    map.get(&aid.to_string())
        .and_then(Value::as_u64)
        .or_else(|| map.get(&aid.to_string()).and_then(Value::as_i64).map(|x| x as u64))
        .unwrap_or(0) as u32
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_ok_when_no_over() {
        let snap = json!({
            "web_pools": {
                "imageDispatchPoolIds": [1, 2],
                "imagePinIds": [1],
                "imagePoolIds": [1],
            },
            "pool_stats": {
                "AvailableByAccount": [{"AccountID": 1, "Count": 1}],
                "ByStatus": {"available": 1}
            },
            "imagine_by_account": {"1": 5, "2": 3}
        });
        let rows = ticket_quota_audit(&snap, true);
        assert!(rows.iter().all(|r| r.ok));
    }
}
