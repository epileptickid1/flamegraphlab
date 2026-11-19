// Adapted from laboratorna2var5.cpp; algorithms and seed are preserved.
#include "kernels.h"
#include <algorithm>
#include <random>
#include <stdexcept>
#include <thread>

__declspec(noinline) std::vector<int> make_data(std::size_t n) {
    std::mt19937 gen(42);
    std::uniform_int_distribution<int> dist(1, 1'000'000);
    std::vector<int> v(n);
    for (auto& x : v) x = dist(gen);
    return v;
}

// Only these public boundaries stay visible in both builds.
// STL internals are free to inline and vectorize; no whole-program /GL.
__declspec(noinline) int sequential_min(const std::vector<int>& v) {
    if (v.empty()) throw std::invalid_argument("empty input");
    return *std::min_element(v.begin(), v.end());
}

__declspec(noinline) int parallel_min(const std::vector<int>& v, int K) {
    if (K < 1 || static_cast<std::size_t>(K) > v.size())
        throw std::invalid_argument("threads must be in [1, n]");
    const std::size_t block = v.size() / K;
    std::vector<int> local_min(K);
    std::vector<std::thread> threads;
    threads.reserve(K);
    try {
        for (int i = 0; i < K; ++i) {
            const std::size_t begin = i * block;
            const std::size_t end = (i == K - 1) ? v.size() : begin + block;
            threads.emplace_back([&, i, begin, end] {
                local_min[i] = *std::min_element(v.begin() + begin, v.begin() + end);
            });
        }
    } catch (...) {
        for (auto& t : threads) t.join();
        throw;
    }
    for (auto& t : threads) t.join();
    return *std::min_element(local_min.begin(), local_min.end());
}
