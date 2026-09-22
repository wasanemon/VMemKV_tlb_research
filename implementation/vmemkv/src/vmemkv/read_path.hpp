// read_path.hpp - T2 base-region read path for VMemKVImpl.
//
// Once a record's offset is below `base_boundary`, its bytes are immutable forever (an
// in-place update targeting the base is redirected out-of-place instead), so a reader
// doesn't need the seqlock protecting the mutable tail and can read straight out of a
// mapping. Get and Scan want *different* readahead policies for that read though (madvise
// is a property of the whole mapping, not of one read), so three distinct mappings/handles
// of the identical bytes exist (`base`, `base_mmap_scan`, `base_mmap_scan_seq`, plus
// `read_fd` -- see T2FlatFile's constructor for how each is set up) -- which one a given
// call uses is decided per record by try_read_base_record()'s switch below. That switch is
// the single place this decision is made, and callers never choose a mapping themselves.
// Span construction itself is ::make_record_view (t2_flat_file.hpp), shared with
// T2FlatFile::at().
#pragma once

#include <sys/mman.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <bit>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <optional>
#include <span>
#include <thread>
#include <vector>

#include "core/bytes.hpp"
#include "core/spin_backoff.hpp"
#include "t1_index/t1_index.hpp"
#include "t2_flat_file/t2_flat_file.hpp"
#include "vmemkv/hooks.hpp"

namespace vmemkv {

// AtFunc: () -> T2RecordView, called fresh on every retry attempt. Required because
// T2FlatFile::at() reads key_len/value_len unsynchronized to size the key/value spans; a view
// built once and reused across retries can carry a stale span size that the version check
// afterward can't catch, since the size was already wrong before the loop started.
//
// CopyFunc's plain (non-atomic) reads of a record's key/value bytes race, in the C++
// abstract-machine sense, with T2FlatFile::update_value_at()'s plain memcpy of the same bytes.
// It's benign by construction: the version check below discards any read that overlapped a
// concurrent write, so a torn read here is never actually used, only retried (same principle
// as a Linux kernel seqlock). See tsan_suppressions.txt for the suppression.
template <typename AtFunc, typename CopyFunc>
inline auto read_t2_record_seqlock(AtFunc &&at_func, CopyFunc &&copy_func) {
  SpinBackoff backoff;
  while (true) {
    const T2RecordView record = at_func();
    auto atomic_version = std::atomic_ref<const uint64_t>(record.header->version);
    uint64_t v1 = atomic_version.load(std::memory_order_acquire);
    if (v1 % 2 != 0) {
      backoff.wait();
      continue;
    }

    auto result = copy_func(record);

    std::atomic_thread_fence(std::memory_order_acquire);
    uint64_t v2 = atomic_version.load(std::memory_order_acquire);
    if (v1 == v2) {
      return result;
    }
  }
}

// Which caller is asking try_read_base_record() below for a base-region record. The only
// thing that determines the mapping/strategy choice in that function's switch.
enum class BaseReader : uint8_t {
  kGet,   // One record per call, effectively random access -- never wants readahead.
  kScan,  // A contiguous key range per call -- benefits from readahead.
};

// Mapping choice for one base-region read, per record. Picking the "wrong" mapping for a
// record's size is only ever a readahead-policy mismatch, never incorrect data: every
// mapping here covers the identical underlying bytes.
template <typename ConfigT>
struct BaseMappingSelector {
  // Scan reads a contiguous key range (~100 records per call), which traverses the base
  // region in ascending offset order, so unlike Get it benefits from kernel readahead:
  // small records (many faults per call) want MADV_SEQUENTIAL's wider window; large
  // records would have one call's readahead overshoot into unrelated neighboring
  // records, so they use the plain/no-advise mapping instead.
  static auto for_scan(const T2Memory *mem, bool is_small) noexcept -> std::byte * {
    if constexpr (ConfigT::UseReadPolicyRandomOnly) {
      return mem->base;
    }
    if constexpr (ConfigT::UseReadPolicySeqOnly) {
      return mem->base_mmap_scan_seq != nullptr ? mem->base_mmap_scan_seq : mem->base;
    }
    return is_small ? mem->base_mmap_scan_seq : mem->base_mmap_scan;
  }

  // Get reads one record per call and is always effectively random access, regardless of
  // size -- speculative readahead never pays off for it. `base` (the primary mapping,
  // already MADV_RANDOM everywhere) already carries exactly that policy. SeqOnly ablation
  // reads through the MADV_SEQUENTIAL mapping instead; RandomOnly is identical to default.
  static auto for_get_small(const T2Memory *mem) noexcept -> std::byte * {
    if constexpr (ConfigT::UseReadPolicySeqOnly) {
      return mem->base_mmap_scan_seq != nullptr ? mem->base_mmap_scan_seq : mem->base;
    }
    return mem->base;
  }

  // Large-record Get under a pinned read policy: the single mapping to read, or nullptr
  // under the default trifecta (which takes the residency-check path instead).
  static auto for_get_large_pinned(const T2Memory *mem) noexcept -> std::byte * {
    if constexpr (ConfigT::UseReadPolicyRandomOnly) {
      return mem->base;
    }
    if constexpr (ConfigT::UseReadPolicySeqOnly) {
      return mem->base_mmap_scan_seq != nullptr ? mem->base_mmap_scan_seq : mem->base;
    }
    return nullptr;
  }
};

// Blind (no mincore, no fallback) direct read through a given base-region mapping -- the
// common tail end of every path in try_read_base_record() below.
inline auto read_base_record_via(std::byte *mapping,
                                 uint64_t offset,
                                 uint64_t base_boundary) -> std::optional<T2RecordView> {
  if (mapping == nullptr) {
    return std::nullopt;
  }
  const std::byte *record_base = mapping + offset;
  const auto *header = reinterpret_cast<const ValueRecordHeader *>(record_base);
  const uint64_t needed = sizeof(ValueRecordHeader) + header->key_len + header->value_len;
  // Defense-in-depth backstop, not a routine path: falls through to the always-correct
  // mmap+seqlock path when it doesn't hold.
  if (offset + needed > base_boundary) {
    return std::nullopt;
  }
  return make_record_view(header);
}

// Warm-path half of BaseReader::kGet's large-record case: if `[offset, offset+read_len)`
// is entirely page-cache resident (checked via mincore(), which -- unlike an actual read --
// never blocks on a fault itself), reads a live span straight out of the mmap for free (no
// syscall, no copy). Returns std::nullopt on any doubt (mincore() unavailable/failed, or any
// page not resident) -- caller falls back to a bounded pread() instead of risking a
// page-fault-driven block here. A page could in theory be evicted between this check and the
// caller reading through the returned span, but base-region bytes are immutable, so that only
// costs an ordinary page fault on the read -- never wrong data.
inline auto try_read_resident_base_record(std::byte *mapping,
                                          uint64_t offset,
                                          uint64_t read_len,
                                          uint64_t base_boundary) -> std::optional<T2RecordView> {
  constexpr uintptr_t kPageSize = 4096;
  constexpr uintptr_t kPageMask = kPageSize - 1;
  std::byte *const record_base = mapping + offset;
  const auto start = reinterpret_cast<uintptr_t>(record_base);
  const auto aligned_start = start & ~kPageMask;
  const auto aligned_len = ((start + read_len + kPageMask) & ~kPageMask) - aligned_start;

  thread_local static std::vector<unsigned char> tl_mincore_vec;
  tl_mincore_vec.resize(aligned_len / kPageSize);
  if (::mincore(reinterpret_cast<void *>(aligned_start), aligned_len, tl_mincore_vec.data()) != 0) {
    return std::nullopt;
  }
  for (unsigned char page_status : tl_mincore_vec) {
    if ((page_status & 1) == 0) {
      return std::nullopt;  // Not resident -- let the caller's pread() fetch it instead.
    }
  }

  const auto *header = reinterpret_cast<const ValueRecordHeader *>(record_base);
  const uint64_t needed = sizeof(ValueRecordHeader) + header->key_len + header->value_len;
  // Defense-in-depth, same role as read_base_record_via()'s identical check.
  if (offset + needed > base_boundary) {
    return std::nullopt;
  }
  return make_record_view(header);
}

// Embedded block-count size hint for a T1 payload, plus margin. The hint is 16-byte-granular
// while records are only 8-byte aligned, so it can undershoot the true aligned length by up
// to 8 bytes; hint + margin covers every record this store can produce, whether resident
// (mmap path) or not (pread path).
inline auto base_size_hint(uint64_t payload_bits) noexcept -> uint64_t {
  constexpr uint64_t kSizeHintMargin = 16;
  return ((payload_bits >> detail::kPayloadSizeShift) * detail::kRecordBlockAlignment) + kSizeHintMargin;
}

// Large-record Get under the default trifecta: check residency first (mincore(), which never
// blocks on a fault itself) before committing to a read -- if resident, base_mmap_scan gives
// a free mmap read costing nothing beyond what a plain mmap-based Get would have paid
// anyway; if not, one bounded pread() beats the N separate page faults an mmap read of a
// multi-page cold record would trigger.
inline auto read_large_get_cold(const T2Memory *mem,
                                uint64_t offset,
                                uint64_t base_boundary,
                                uint64_t size_hint,
                                std::vector<std::byte> *cold_buf) -> std::optional<T2RecordView> {
  const uint64_t read_len = std::min(size_hint, base_boundary - offset);
  if (mem->base_mmap_scan != nullptr) {
    if (auto resident = try_read_resident_base_record(mem->base_mmap_scan, offset, read_len, base_boundary);
        resident.has_value()) {
      return resident;
    }
  }
  if (mem->read_fd < 0 || cold_buf == nullptr) {
    return std::nullopt;
  }
  cold_buf->resize(read_len);
  const ssize_t bytes_read = ::pread(mem->read_fd, cold_buf->data(), read_len, static_cast<off_t>(offset));
  if (bytes_read < static_cast<ssize_t>(sizeof(ValueRecordHeader))) {
    return std::nullopt;  // Short read or error -- fall back to the always-correct seqlock path.
  }
  const auto *header = reinterpret_cast<const ValueRecordHeader *>(cold_buf->data());
  const uint64_t needed = sizeof(ValueRecordHeader) + header->key_len + header->value_len;
  // Defense-in-depth, same role as read_base_record_via()'s identical check.
  if (needed > static_cast<uint64_t>(bytes_read)) {
    return std::nullopt;
  }
  return make_record_view(header);
}

// Single entry point for every T2 base-region read. Computes the shared preamble (offset,
// base_boundary check, embedded size hint) once, then dispatches on the reader -- see
// BaseMappingSelector for why each reader picks the mapping it does.
//
// `payload_bits` (not pre-masked to an offset) is required so the T1 index's embedded
// block-count size hint can size the read. `cold_buf`: only used by BaseReader::kGet's large,
// non-resident case (a bounded pread() destination, resized as needed; caller must keep it
// alive as long as the returned view is used) -- pass nullptr for BaseReader::kScan, which
// never needs it. Returns std::nullopt whenever this fast path isn't available (offset still
// in the mutable tail, a mapping/fd wasn't created, mincore()/pread() failed, or a
// defense-in-depth bounds check fails) -- callers fall back to t2_.at() +
// read_t2_record_seqlock() in that case.
template <typename ConfigT>
inline auto try_read_base_record(const T2Memory *mem,
                                 uint64_t payload_bits,
                                 BaseReader reader,
                                 std::vector<std::byte> *cold_buf) -> std::optional<T2RecordView> {
  const uint64_t offset = payload_bits & detail::kPayloadOffsetMask;
  const uint64_t base_boundary = mem->base_boundary.load(std::memory_order_acquire);
  if (offset >= base_boundary) {
    return std::nullopt;
  }

  constexpr uint64_t kPageSize = 4096;
  const uint64_t size_hint = base_size_hint(payload_bits);
  const bool is_small = size_hint <= kPageSize;
  using Selector = BaseMappingSelector<ConfigT>;

  switch (reader) {
    case BaseReader::kScan:
      return read_base_record_via(Selector::for_scan(mem, is_small), offset, base_boundary);
    case BaseReader::kGet:
      if (is_small) {
        return read_base_record_via(Selector::for_get_small(mem), offset, base_boundary);
      }
      if (std::byte *pinned = Selector::for_get_large_pinned(mem); pinned != nullptr) {
        return read_base_record_via(pinned, offset, base_boundary);
      }
      return read_large_get_cold(mem, offset, base_boundary, size_hint, cold_buf);
  }
  assert(false && "unhandled BaseReader");
  return std::nullopt;
}

// Recovers an inlined key's length from its zero-padded 16-byte prefix by trimming trailing
// zero bytes. Only unambiguous because inlining refuses keys whose own last byte is 0x00.
inline auto inline_key_len(std::span<const std::byte> index_key) noexcept -> size_t {
  static_assert(kStoreKeyBytes == 2 * sizeof(uint64_t));
  static_assert(std::endian::native == std::endian::little);
  uint64_t lo_word;
  uint64_t hi_word;
  std::memcpy(&lo_word, index_key.data(), sizeof(lo_word));
  std::memcpy(&hi_word, index_key.data() + sizeof(lo_word), sizeof(hi_word));
  return hi_word != 0 ? sizeof(lo_word) + (std::bit_width(hi_word) + 7) / 8 : (std::bit_width(lo_word) + 7) / 8;
}

// Copies a T1-inlined value's bytes out of its payload word into the caller's buffer.
inline void copy_inline_value(uint64_t payload_bits, size_t size, std::byte *out) noexcept {
  std::memcpy(out, &payload_bits, size);
}

inline auto byte_span_equal(std::span<const std::byte> lhs, std::span<const std::byte> rhs) noexcept -> bool {
  return vmemkv::bytes_equal(lhs, rhs);
}

inline auto key_in_range(std::span<const std::byte> key,
                         std::span<const std::byte> lower_bound,
                         std::span<const std::byte> upper_bound) noexcept -> bool {
  return vmemkv::bytes_in_range(key, lower_bound, upper_bound);
}

// Whether both scan bounds decide range membership identically on 16-byte padded
// prefixes and on trimmed keys: no 0x00 in the first 16 bytes and length at most 16.
// Inlined keys are at most 16 bytes and never end in 0x00, so under clean bounds the
// sorted walk's prefix containment implies full-key containment and the per-entry
// recheck can be skipped. Evaluated once per scan call -- the bounds are constant
// across all its entries.
inline auto scan_bounds_clean(std::span<const std::byte> lower_bound,
                              std::span<const std::byte> upper_bound) noexcept -> bool {
  auto bound_is_clean = [](std::span<const std::byte> bound) noexcept {
    if (bound.size() > kStoreKeyBytes) {
      return false;
    }
    for (size_t i = 0; i < bound.size(); ++i) {
      if (bound[i] == std::byte{0}) {
        return false;
      }
    }
    return true;
  };
  return bound_is_clean(lower_bound) && bound_is_clean(upper_bound);
}

// Lowers ShardedT1Index's maintenance soft threshold for the scan's duration, keeping append
// regions L2-cache-sized while the scan linearly passes over them.
template <typename T1>
struct ScanActiveGuard {
  const T1 &t1;
  explicit ScanActiveGuard(const T1 &t1) : t1(t1) { t1.set_scan_active(true); }
  ~ScanActiveGuard() { t1.set_scan_active(false); }
};

}  // namespace vmemkv
