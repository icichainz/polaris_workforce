//// Shared ingest state, in public ETS tables so every HTTP handler can read
//// and write it without going through one process:
////
//// - site configs, pushed by Django;
//// - the running site actors;
//// - the dedupe keys (`client_uuid`s and gateway batch ids, rule 7).
////
//// The tables belong to the process that called `new` (the tracker's main
//// process, or a test), so they outlive restarts of the hub: a hub crash no
//// longer loses the site configs.

import gleam/erlang/process.{type Subject}
import polaris_tracker/domain.{type SiteConfig}
import polaris_tracker/site

pub type Registry

@external(erlang, "polaris_tracker_ffi", "registry_new")
pub fn new() -> Registry

/// Records a dedupe key. `False` when it was already known.
@external(erlang, "polaris_tracker_ffi", "registry_remember")
pub fn remember(registry: Registry, key: String, now: Int) -> Bool

/// Forgets keys recorded before `cutoff`; returns how many.
@external(erlang, "polaris_tracker_ffi", "registry_prune_seen")
pub fn prune_seen(registry: Registry, cutoff: Int) -> Int

@external(erlang, "polaris_tracker_ffi", "registry_seen_size")
pub fn seen_size(registry: Registry) -> Int

@external(erlang, "polaris_tracker_ffi", "registry_put_config")
pub fn put_config(registry: Registry, site_id: Int, config: SiteConfig) -> Nil

@external(erlang, "polaris_tracker_ffi", "registry_get_config")
pub fn get_config(registry: Registry, site_id: Int) -> Result(SiteConfig, Nil)

@external(erlang, "polaris_tracker_ffi", "registry_count_configs")
pub fn count_configs(registry: Registry) -> Int

@external(erlang, "polaris_tracker_ffi", "registry_put_running")
pub fn put_running(
  registry: Registry,
  site_id: Int,
  site: Subject(site.Message),
) -> Nil

@external(erlang, "polaris_tracker_ffi", "registry_get_running")
pub fn get_running(
  registry: Registry,
  site_id: Int,
) -> Result(Subject(site.Message), Nil)

@external(erlang, "polaris_tracker_ffi", "registry_remove_running")
pub fn remove_running(registry: Registry, site_id: Int) -> Nil

@external(erlang, "polaris_tracker_ffi", "registry_clear_running")
pub fn clear_running(registry: Registry) -> Nil
