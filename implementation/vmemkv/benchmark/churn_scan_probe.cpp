// Standalone scan-layout probe (bypasses Google Benchmark): builds a VMemKV
// corpus in a chosen physical order, checkpoints it, optionally re-inserts the
// head of the key space (CHURN_HEAD_PCT percent -> fresh versions appended at
// the T2 tail), then runs a fixed duration of multi-threaded uniform-range
// scans and prints the achieved ops/s.
//
// LAYOUT=shuffled inserts keys in a seeded-shuffled order, so after
// checkpoint() the T2 base region is physically scattered relative to key
// order. LAYOUT=ordered produces the key-ordered layout the regular benchmark
// masters have; with CHURN_HEAD_PCT it becomes the mixed base+tail layout
// real updates/deletes produce between reorganizes.
//
// Comparing scan read-submission designs (per-record vs batched) means
// building this binary twice from the respective trees.
//
// Build:   cmake --build build-rel --target churn_scan_probe
// Run:     VMEMKV_DB_DIR=/path LAYOUT=ordered CHURN_HEAD_PCT=20 \
//          ./build-rel/benchmark/churn_scan_probe

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <numeric>
#include <random>
#include <span>
#include <string>
#include <thread>
#include <tuple>
#include <vector>
#include <vmemkv/config.hpp>

#include "api/store_adapter.hpp"
#include "vmemkv_impl.hpp"

namespace {

using Store = vmemkv::StoreAdapter<vmemkv::VMemKVImpl<vmemkv::Config<vmemkv::BloomFilter, vmemkv::T1InlineValue>>>;

constexpr std::size_t kScanCount = 100;
constexpr std::size_t kMixEveryNth = 5;
constexpr std::size_t kInlineValueBytes = 8;

auto env_size(const char *name, std::size_t fallback) -> std::size_t {
  const char *raw = std::getenv(name);
  if (raw == nullptr || *raw == '\0') {
    return fallback;
  }
  return std::strtoull(raw, nullptr, 10);
}

auto env_string(const char *name, const char *fallback) -> std::string {
  const char *raw = std::getenv(name);
  return (raw == nullptr || *raw == '\0') ? std::string(fallback) : std::string(raw);
}

auto make_key(std::size_t index) -> std::string {
  std::array<char, 17> buffer{};
  std::snprintf(buffer.data(), buffer.size(), "k%015zx", index);
  return std::string(buffer.data());
}

auto make_value(std::size_t index, std::size_t target_size) -> std::string {
  const std::size_t size =
      (target_size != kInlineValueBytes && index % kMixEveryNth == 0) ? kInlineValueBytes : target_size;
  std::string value(size, '\0');
  uint64_t state = static_cast<uint64_t>(index) * 0x9E3779B97F4A7C15ULL + 1;
  for (std::size_t offset = 0; offset < size; offset += sizeof(uint64_t)) {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    std::memcpy(value.data() + offset, &state, std::min(sizeof(uint64_t), size - offset));
  }
  return value;
}

auto touch_bytes(std::span<const std::byte> value) noexcept -> uint64_t {
  uint64_t checksum = 0;
  for (std::byte b : value) {
    checksum = (checksum * 131) + static_cast<uint8_t>(b);
  }
  return checksum;
}

}  // namespace

int main() {
  const std::size_t key_count = env_size("CHURN_KEYS", 131040);
  const std::size_t value_size = env_size("CHURN_VALUE", 65536);
  const std::size_t threads = env_size("CHURN_THREADS", 32);
  const double seconds = static_cast<double>(env_size("CHURN_SECONDS", 8));
  const std::string layout = env_string("LAYOUT", "shuffled");
  const std::size_t churn_head_pct = env_size("CHURN_HEAD_PCT", 0);
  const std::string path = env_string("VMEMKV_DB_DIR", ".") + "/churn_probe_" + layout +
                           (churn_head_pct > 0 ? "_churn" + std::to_string(churn_head_pct) : "");

  std::error_code ignored;
  std::filesystem::remove(path, ignored);
  vmemkv::remove_wal_segments(vmemkv::derive_wal_path(path));
  std::filesystem::remove(vmemkv::derive_manifest_path(path), ignored);
  std::filesystem::remove(vmemkv::derive_t1_chk_path(path), ignored);
  std::filesystem::remove(vmemkv::derive_t2_chk_path(path), ignored);

  setenv("VMEMKV_SUPPRESS_AUTO_REORG", "1", 1);
  auto store = std::make_unique<Store>(path, Store::ConfigType::DefaultT2CapacityBytes);
  std::vector<std::size_t> order(key_count);
  std::ranges::iota(order, std::size_t{0});
  if (layout == "shuffled") {
    std::mt19937_64 rng(42);
    std::shuffle(order.begin(), order.end(), rng);
  }
  const auto setup_start = std::chrono::steady_clock::now();
  store->bulk_load(
      key_count,
      [&order](std::size_t i) { return make_key(order[i]); },
      [&order, value_size](std::size_t i) { return make_value(order[i], value_size); });
  store->checkpoint();
  store.reset();
  const double setup_sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - setup_start).count();
  unsetenv("VMEMKV_SUPPRESS_AUTO_REORG");

  auto scan_store = std::make_unique<Store>(path, Store::ConfigType::DefaultT2CapacityBytes);

  // Partial-churn mode: re-insert the first CHURN_HEAD_PCT percent of the key space
  // (remove + insert appends a fresh version at the T2 tail), leaving the remaining
  // base region key-ordered -- the mixed layout real updates/deletes produce.
  double churn_sec = 0.0;
  if (churn_head_pct > 0) {
    const std::size_t churn_count = key_count * churn_head_pct / 100;
    const auto churn_start = std::chrono::steady_clock::now();
    for (std::size_t i = 0; i < churn_count; ++i) {
      scan_store->remove(make_key(i));
    }
    scan_store->bulk_load(
        churn_count,
        [](std::size_t i) { return make_key(i); },
        [value_size](std::size_t i) { return make_value(i, value_size); });
    churn_sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - churn_start).count();
  }

  std::atomic<std::uint64_t> total_ops{0};
  std::atomic<uint64_t> sink{0};
  std::vector<std::thread> workers;
  const auto run_start = std::chrono::steady_clock::now();
  for (std::size_t t = 0; t < threads; ++t) {
    workers.emplace_back([&, t] {
      std::mt19937_64 rng(42 + t);
      std::uniform_int_distribution<std::size_t> start_index(0, key_count - kScanCount - 1);
      std::uint64_t local_ops = 0;
      std::uint64_t local_sum = 0;
      while (std::chrono::steady_clock::now() - run_start < std::chrono::duration<double>(seconds)) {
        const std::size_t start = start_index(rng);
        std::ignore = scan_store->scan(make_key(start),
                                       make_key(start + kScanCount - 1),
                                       [&local_sum](std::span<const std::byte>, std::span<const std::byte> value) {
                                         local_sum += touch_bytes(value);
                                       });
        ++local_ops;
      }
      sink.fetch_add(local_ops + local_sum, std::memory_order_relaxed);
      total_ops.fetch_add(local_ops, std::memory_order_relaxed);
    });
  }
  for (auto &w : workers) {
    w.join();
  }
  const double run_sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - run_start).count();
  const double ops_per_sec = static_cast<double>(total_ops.load()) / run_sec;

  std::printf(
      "PROBE layout=%s churn_pct=%zu keys=%zu value=%zu threads=%zu setup_sec=%.1f churn_sec=%.1f "
      "run_sec=%.1f ops=%.0f ops_per_sec=%.0f\n",
      layout.c_str(),
      churn_head_pct,
      key_count,
      value_size,
      threads,
      setup_sec,
      churn_sec,
      run_sec,
      static_cast<double>(total_ops.load()),
      ops_per_sec);
  return 0;
}
