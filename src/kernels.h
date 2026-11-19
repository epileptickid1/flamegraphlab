#pragma once
#include <cstddef>
#include <vector>
std::vector<int> make_data(std::size_t n);
int sequential_min(const std::vector<int>& values);
int parallel_min(const std::vector<int>& values, int threads);
