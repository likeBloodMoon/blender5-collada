// OpenCOLLADA-compatible float32/int32 array formatter & parser (C++17, plain C ABI)
// Format rules: "0" for +/-0/NaN/inf; %.6g when |v|<1e-4 or >=1e7, else %.7g; "e-05" -> "e-5"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <thread>
#include <vector>

#if defined(_WIN32)
#define COLLADA_API __declspec(dllexport)
#else
#define COLLADA_API __attribute__((visibility("default")))
#endif

extern "C" COLLADA_API size_t fmt_f32_array(const float* a, size_t n, char* out) {
    char* p = out;
    for (size_t i = 0; i < n; ++i) {
        if (i) *p++ = ' ';
        double v = a[i];
        if (!std::isfinite(v) || v == 0.0) { *p++ = '0'; continue; }
        double av = std::fabs(v);
        int prec = (av < 1e-4 || av >= 1e7) ? 6 : 7;
        char* start = p;
        p = std::to_chars(p, p + 32, v, std::chars_format::general, prec).ptr;
        // strip leading zero of exponent: e-05 -> e-5, e+07 -> e+7
        for (char* q = start; q < p; ++q) {
            if (*q == 'e' && q + 2 < p && q[2] == '0' && q + 3 < p) {
                std::memmove(q + 2, q + 3, static_cast<size_t>(p - (q + 3)));
                --p;
                break;
            }
        }
    }
    return static_cast<size_t>(p - out);
}

extern "C" COLLADA_API size_t fmt_f32_array_mt(const float* a, size_t n, char* out, int nthreads) {
    if (n < 200000 || nthreads <= 1) return fmt_f32_array(a, n, out);
    size_t chunk = (n + static_cast<size_t>(nthreads) - 1) / static_cast<size_t>(nthreads);
    std::vector<std::vector<char>> bufs(static_cast<size_t>(nthreads));
    std::vector<size_t> lens(static_cast<size_t>(nthreads), 0);
    std::vector<std::thread> th;
    for (int t = 0; t < nthreads; ++t) {
        size_t s = static_cast<size_t>(t) * chunk;
        size_t e = std::min(n, s + chunk);
        if (s >= e) break;
        th.emplace_back([&, t, s, e] {
            bufs[t].resize((e - s) * 16 + 1);
            lens[t] = fmt_f32_array(a + s, e - s, bufs[t].data());
        });
    }
    for (auto& x : th) x.join();
    char* p = out;
    for (size_t t = 0; t < th.size(); ++t) {
        if (t) *p++ = ' ';
        std::memcpy(p, bufs[t].data(), lens[t]);
        p += lens[t];
    }
    return static_cast<size_t>(p - out);
}

extern "C" COLLADA_API size_t fmt_i32_array(const int32_t* a, size_t n, char* out) {
    char* p = out;
    for (size_t i = 0; i < n; ++i) {
        if (i) *p++ = ' ';
        p = std::to_chars(p, p + 16, a[i]).ptr;
    }
    return static_cast<size_t>(p - out);
}

extern "C" COLLADA_API size_t fmt_i32_array_mt(const int32_t* a, size_t n, char* out, int nthreads) {
    if (n < 400000 || nthreads <= 1) return fmt_i32_array(a, n, out);
    size_t chunk = (n + static_cast<size_t>(nthreads) - 1) / static_cast<size_t>(nthreads);
    std::vector<std::vector<char>> bufs(static_cast<size_t>(nthreads));
    std::vector<size_t> lens(static_cast<size_t>(nthreads), 0);
    std::vector<std::thread> th;
    for (int t = 0; t < nthreads; ++t) {
        size_t s = static_cast<size_t>(t) * chunk;
        size_t e = std::min(n, s + chunk);
        if (s >= e) break;
        th.emplace_back([&, t, s, e] {
            bufs[t].resize((e - s) * 12 + 1);
            lens[t] = fmt_i32_array(a + s, e - s, bufs[t].data());
        });
    }
    for (auto& x : th) x.join();
    char* p = out;
    for (size_t t = 0; t < th.size(); ++t) {
        if (t) *p++ = ' ';
        std::memcpy(p, bufs[t].data(), lens[t]);
        p += lens[t];
    }
    return static_cast<size_t>(p - out);
}

// Parse whitespace-separated floats (for <float_array>): returns count parsed
extern "C" COLLADA_API size_t parse_f32(const char* s, size_t len, float* out, size_t cap) {
    const char* p = s;
    const char* e = s + len;
    size_t n = 0;
    while (p < e && n < cap) {
        while (p < e && (*p == ' ' || *p == '\n' || *p == '\t' || *p == '\r')) ++p;
        if (p >= e) break;
        auto r = std::from_chars(p, e, out[n]);
        if (r.ec != std::errc()) break;
        ++n;
        p = r.ptr;
    }
    return n;
}

// Parse whitespace-separated int32s (for <p> / <vcount>): returns count parsed
extern "C" COLLADA_API size_t parse_i32(const char* s, size_t len, int32_t* out, size_t cap) {
    const char* p = s;
    const char* e = s + len;
    size_t n = 0;
    while (p < e && n < cap) {
        while (p < e && (*p == ' ' || *p == '\n' || *p == '\t' || *p == '\r')) ++p;
        if (p >= e) break;
        auto r = std::from_chars(p, e, out[n]);
        if (r.ec != std::errc()) break;
        ++n;
        p = r.ptr;
    }
    return n;
}
