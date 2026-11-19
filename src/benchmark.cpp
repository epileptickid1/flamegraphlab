#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <psapi.h>
#include "kernels.h"
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

using Clock = std::chrono::steady_clock;
static std::uint64_t ticks(FILETIME t) {
    return (static_cast<std::uint64_t>(t.dwHighDateTime) << 32) | t.dwLowDateTime;
}
static std::uint64_t unix_us() {
    FILETIME t; GetSystemTimePreciseAsFileTime(&t);
    return (ticks(t) - 116444736000000000ULL) / 10;
}
struct Counters {
    std::uint64_t user, kernel, cycles;
    PROCESS_MEMORY_COUNTERS_EX memory{};
};
static Counters counters() {
    Counters c{};
    FILETIME created, exited, kernel, user;
    if (!GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user)
        || !QueryProcessCycleTime(GetCurrentProcess(), &c.cycles)
        || !GetProcessMemoryInfo(GetCurrentProcess(),
            reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&c.memory), sizeof(c.memory)))
        throw std::runtime_error("Cannot read Windows process counters");
    c.user = ticks(user); c.kernel = ticks(kernel);
    return c;
}
static std::uint64_t positive(const char* text) {
    std::string s(text);
    if (s.empty() || s.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("expected a positive integer");
    auto result = std::stoull(s);
    if (!result) throw std::invalid_argument("zero is not allowed");
    return result;
}
static void self_test() {
    const std::vector<std::vector<int>> cases{
        {42}, {5, 1, 8, 2}, {9, 7, 5, 3, -4}, {2, 2, 2},
        {std::numeric_limits<int>::max(), 0, std::numeric_limits<int>::min()}};
    const int expected[]{42, 1, -4, 2, std::numeric_limits<int>::min()};
    for (std::size_t i = 0; i < cases.size(); ++i) {
        if (sequential_min(cases[i]) != expected[i]) throw std::runtime_error("seq test failed");
        for (int k = 1; k <= static_cast<int>(cases[i].size()); ++k)
            if (parallel_min(cases[i], k) != expected[i]) throw std::runtime_error("parallel test failed");
    }
    const auto a = make_data(101), b = make_data(101);
    if (a != b) throw std::runtime_error("seed is not reproducible");
    try { sequential_min({}); throw std::runtime_error("empty accepted"); }
    catch (const std::invalid_argument&) {}
    try { parallel_min({1}, 2); throw std::runtime_error("invalid K accepted"); }
    catch (const std::invalid_argument&) {}
    std::cout << "{\"self_test\":\"passed\",\"fixtures\":5}\n";
}
int main(int argc, char** argv) {
    try {
        std::size_t n = 10'000'000;
        std::uint64_t repeats = 100, batches = 40;
        int threads = 4;
        std::string mode = "sequential";
        for (int i = 1; i < argc; ++i) {
            std::string arg(argv[i]);
            if (arg == "--self-test") { self_test(); return 0; }
            if (arg == "--help") {
                std::cout << "benchmark --n N --repeats R --batches B --mode sequential|parallel --threads K\n";
                return 0;
            }
            if (i + 1 >= argc) throw std::invalid_argument("missing option value");
            if (arg == "--n") n = static_cast<std::size_t>(positive(argv[++i]));
            else if (arg == "--repeats") repeats = positive(argv[++i]);
            else if (arg == "--batches") batches = positive(argv[++i]);
            else if (arg == "--threads") {
                const auto k = positive(argv[++i]);
                if (k > 256) throw std::invalid_argument("threads must be <= 256");
                threads = static_cast<int>(k);
            }
            else if (arg == "--mode") mode = argv[++i];
            else throw std::invalid_argument("unknown option: " + arg);
        }
        if (mode != "sequential" && mode != "parallel") throw std::invalid_argument("invalid mode");
        if (n > 250'000'000 || repeats > 1'000'000 || batches > 10'000)
            throw std::invalid_argument("workload exceeds safety limit");
        if (mode == "parallel" && static_cast<std::size_t>(threads) > n)
            throw std::invalid_argument("threads exceed n");
        const auto before = counters();
        const auto start_us = unix_us();
        const auto t0 = Clock::now();
        std::vector<int> data;
        std::int64_t checksum = 0;
        int minimum = 0;
        double generation_s = 0, search_s = 0;
        for (std::uint64_t batch = 0; batch < batches; ++batch) {
            const auto batch_start = Clock::now();
            data = make_data(n);
            const auto search_start = Clock::now();
            for (std::uint64_t r = 0; r < repeats; ++r) {
                minimum = mode == "sequential" ? sequential_min(data) : parallel_min(data, threads);
                checksum += minimum;
            }
            const auto search_end = Clock::now();
            generation_s += std::chrono::duration<double>(search_start - batch_start).count();
            search_s += std::chrono::duration<double>(search_end - search_start).count();
        }
        const auto t2 = Clock::now();
        const auto end_us = unix_us();
        const auto after = counters();
        // Independent reference and input fingerprint, outside the measured workload.
        int reference = std::numeric_limits<int>::max();
        std::uint64_t input_sum = 0;
        for (int value : data) {
            if (value < reference) reference = value;
            input_sum += static_cast<std::uint64_t>(value);
        }
        if (minimum != reference || checksum != static_cast<std::int64_t>(reference) * static_cast<std::int64_t>(repeats * batches))
            throw std::runtime_error("result verification failed");
        auto seconds = [](auto a, auto b) { return std::chrono::duration<double>(b - a).count(); };
        std::cout << std::fixed << std::setprecision(9)
            << "{\n  \"n\":" << n << ",\n  \"repeats\":" << repeats << ",\n  \"batches\":" << batches
            << ",\n  \"mode\":\"" << mode << "\",\n  \"threads\":" << (mode == "sequential" ? 1 : threads)
            << ",\n  \"seed\":42,\n  \"minimum\":" << minimum << ",\n  \"checksum\":" << checksum
            << ",\n  \"input_sum\":" << input_sum << ",\n  \"verified\":true"
            << ",\n  \"start_unix_us\":" << start_us << ",\n  \"end_unix_us\":" << end_us
            << ",\n  \"generation_s\":" << generation_s
            << ",\n  \"search_s\":" << search_s
            << ",\n  \"workload_s\":" << seconds(t0, t2)
            << ",\n  \"user_s\":" << (after.user - before.user) / 1e7
            << ",\n  \"kernel_s\":" << (after.kernel - before.kernel) / 1e7
            << ",\n  \"process_cycles\":" << after.cycles - before.cycles
            << ",\n  \"page_faults\":" << after.memory.PageFaultCount - before.memory.PageFaultCount
            << ",\n  \"peak_working_set_bytes\":" << after.memory.PeakWorkingSetSize
            << ",\n  \"private_bytes\":" << after.memory.PrivateUsage << "\n}\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << '\n'; return 1;
    }
}
