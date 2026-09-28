// test_incremental_checkpoint.cpp — Incremental T1 checkpoint publish (reusing clean
// shards' sections from the previous file instead of remerging and rewriting them).
//
// Single-shard coverage at store level (production split thresholds are far above unit-test
// key counts): a clean re-checkpoint must reuse every section, writes must force a rewrite,
// and a crash after a copy-path checkpoint must recover fully via WAL replay convergence.
// Multi-shard selective behavior of the 4-argument checkpoint_all_shards() overload lives in
// test_sharded_t1_index.cpp, next to the split mechanics that produce multiple shards.

#include <doctest/doctest.h>

#include <cstdint>
#include <memory>
#include <string>
#include <vmemkv/vmemkv.hpp>

#include "test_support.hpp"

namespace {

constexpr uint64_t kStoreCapacityBytes = 64ULL * 1024 * 1024;

template <typename Store>
void insert_range(Store &store, int first, int key_count) {
  for (int i = 0; i < key_count; ++i) {
    const int key = first + i;
    REQUIRE(store.insert("k" + std::to_string(key), vmemkv_test::indexed_value(key)));
  }
}

template <typename Store>
void check_range(Store &store, int first, int key_count) {
  for (int i = 0; i < key_count; ++i) {
    const int key = first + i;
    const auto bytes = vmemkv_test::get_optional_bytes(&store, "k" + std::to_string(key));
    REQUIRE(bytes.has_value());
    CHECK(vmemkv_test::span_to_string(vmemkv_test::as_span(*bytes)) == vmemkv_test::indexed_value(key));
  }
}

}  // namespace

#define INCREMENTAL_STORE_TYPES \
  vmemkv::variants::VMemKV_Var0_Baseline, vmemkv::variants::VMemKV_Var1_Bloom, vmemkv::variants::VMemKV_Var2_Inline

TEST_CASE_TEMPLATE("incremental checkpoint: checkpoint with no intervening writes reuses every section",
                   Store,
                   INCREMENTAL_STORE_TYPES) {
  const auto path = vmemkv_test::ScopedTempPath("vmemkv_incr", vmemkv_test::remove_store_files);
  auto store = std::make_unique<Store>(path, kStoreCapacityBytes);
  insert_range(*store, 0, 100);
  store->checkpoint();
  const auto stats_full = store->get_statistics();
  REQUIRE(stats_full.last_checkpoint_t1_shard_count == 1);
  CHECK(stats_full.last_checkpoint_t1_shards_reused == 0);

  store->checkpoint();
  const auto stats_reused = store->get_statistics();
  CHECK(stats_reused.last_checkpoint_t1_shard_count == 1);
  CHECK(stats_reused.last_checkpoint_t1_shards_reused == 1);
  check_range(*store, 0, 100);
}

TEST_CASE_TEMPLATE("incremental checkpoint: writes force a rewrite of the dirty shard",
                   Store,
                   INCREMENTAL_STORE_TYPES) {
  const auto path = vmemkv_test::ScopedTempPath("vmemkv_incr", vmemkv_test::remove_store_files);
  auto store = std::make_unique<Store>(path, kStoreCapacityBytes);
  insert_range(*store, 0, 100);
  store->checkpoint();
  store->checkpoint();
  REQUIRE(store->get_statistics().last_checkpoint_t1_shards_reused == 1);

  insert_range(*store, 100, 100);
  store->checkpoint();
  const auto stats = store->get_statistics();
  CHECK(stats.last_checkpoint_t1_shard_count == 1);
  CHECK(stats.last_checkpoint_t1_shards_reused == 0);
  check_range(*store, 0, 200);
}

TEST_CASE_TEMPLATE("incremental checkpoint: crash after a copy-path checkpoint recovers everything",
                   Store,
                   INCREMENTAL_STORE_TYPES) {
  const auto path = vmemkv_test::ScopedTempPath("vmemkv_incr", vmemkv_test::remove_store_files);
  {
    auto store = std::make_unique<Store>(path, kStoreCapacityBytes);
    insert_range(*store, 0, 100);
    store->checkpoint();
    store->checkpoint();
    // The second checkpoint took the copy path; the tail below is WAL-only.
    REQUIRE(store->get_statistics().last_checkpoint_t1_shards_reused == 1);
    insert_range(*store, 100, 50);
  }
  {
    auto store = std::make_unique<Store>(path, kStoreCapacityBytes);
    check_range(*store, 0, 150);
  }
}
