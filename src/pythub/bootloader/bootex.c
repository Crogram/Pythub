/*
 * bootex.c — Pythub 自研单文件引导器
 *
 * 一套源码，两种编译目标：
 *   Windows : x86_64-w64-mingw32-gcc -O2 -s -Wall -mconsole -lshell32 -o bootex.exe bootex.c
 *             （windowed 应用换 -mwindows -DGUI_APP=1: 无控制台，致命错误走消息框）
 *   POSIX   : cc -O2 -Wall -o bootex bootex.c
 *             （POSIX 分支用于 mac/linux 本机全流程自测，也可产出原生 ELF/Mach-O 单文件）
 *
 * 归档文件结构（由 pythub.exe_archive.make_exe 生成，全部自研，无第三方格式依赖）:
 *   [ 引导器本体 ][ 文件数据区 ][ TOC ][ 96 字节 cookie ]
 *
 * cookie 布局（小端）:
 *   0..7   magic "PYTHUBEX"
 *   8..15  payload_offset  文件数据区起始（绝对偏移）
 *   16..23 toc_offset      TOC 起始（绝对偏移）
 *   24..31 toc_size        TOC 长度
 *   32..63 md5hex          (数据区 + TOC) 的 MD5，用作解压缓存目录键
 *   64..95 app[32]         应用名（净化后的缓存安全名），运行时缓存按应用分组:
 *                          <缓存>/exe/<app>/<md5>，供同应用旧版本 LRU 清理识别
 *
 * TOC 布局:
 *   u32 条目数；每条目: u32 path_len, u32 stored_size, u32 raw_size,
 *                      u64 data_offset(绝对), u8 flags, path(UTF-8, POSIX 分隔符)
 *   flags: 0 = 原样存储, 1 = zlib(DEFLATE) 压缩 —— C 端用内嵌的 miniz(tinfl)
 *          解压（Python 端 zlib.compress 输出即 zlib 包装的 DEFLATE 流）。
 *
 * 运行逻辑（参考 PyInstaller CArchive/bootloader 的思路独立实现）:
 *   读取自身 → 校验尾部 cookie → 归档体 MD5 完整性校验（防篡改/截断）
 *   → 解压到持久缓存目录（已解压则校验缓存启动器 MD5，被篡改自动重新解压）
 *   → 调起内嵌解释器执行 app.pyz（透传命令行参数、返回退出码）
 *
 * 解压可靠性:
 *   - 原子解压: 先解压到 <缓存>.part 临时目录，校验启动器存在后原子改名就位，
 *     中途中断不会留下半成品缓存目录；
 *   - 并发互斥: 首次解压前获取进程级锁（Windows 命名互斥体 / POSIX flock），
 *     拿到锁后双重检查缓存，避免多实例并发解压互相踩踏。
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <stdint.h>
#include <sys/stat.h>
#include <sys/types.h>

#include "miniz_tinfl.h" /* 内嵌 miniz tinfl(vendored, MIT): 仅用 inflate 解压 zlib(DEFLATE) 条目 */

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#else
#include <unistd.h>
#include <limits.h>
#include <fcntl.h>
#include <dirent.h>
#include <signal.h>
#include <dlfcn.h> /* embed 模式: 进程内 dlopen libpython（需求 64） */
#include <sys/file.h>
#include <sys/wait.h>
#endif

#define COOKIE_SIZE 96
#define APP_FIELD_SIZE 32
#define PYZ_REL "app.pyz"
#define FLAG_DEFLATE 1 /* 条目经 zlib 压缩 */
#define FLAG_SYMLINK 2 /* 条目为符号链接，内容为链接目标串（POSIX 重建） */
/* FLAG_MEM(4): 内存载荷条目 —— 解压时跳过（不落盘），仅在内存中解出后
 * 引导器源码经 PYTHUB_BOOT 环境变量注入、应用字节码经 fd 3 管道直传
 * 解释器（Windows 为句柄值经命令行传入），全程内存执行 */
#define FLAG_MEM 4

/* mem 模式 TOC 条目路径 */
#define MEM_BOOT_REL "app.boot" /* Python 引导器源码 */
#define MEM_PAYLOAD_REL "app.mem" /* 应用字节码（裸 marshal） */

/* embed 内嵌解释器模式（需求 64）TOC 条目: FLAG_MEM 载荷（解压不落盘），
 * embed 分支主动写到缓存后 dlopen libpython 进程内初始化解释器。
 * "Python 进内存"的 OS 层最佳近似: 引导器进程即 Python 进程，无需解压
 * 独立 python 可执行目录；落盘仅 libpython + stdlib.zip 两个文件。 */
#define EMBED_PYLIB_REL "app.pylib"  /* libpython 共享库（PBS 运行时） */
#define EMBED_STDLZ_REL "app.stdlz"  /* stdlib zip（zipimport 直接导入） */
#define EMBED_STDLZF_REL "app.stdlzf" /* stdlib frozen 字节码（static 模式） */
#define EMBED_TCLTK_REL "app.tcltk"  /* Tcl/Tk 数据目录 zip（可选, 引导器落盘,
                                      * runner 释放为真实文件并设 TCL_LIBRARY） */
#define APP_WDLLS_REL "app.wdlls"    /* Windows static 第三方依赖 DLL zip
                                      * （openssl/sqlite3 导入库对应 DLL,
                                      * runner 释放到缓存供 PATH 搜索） */
#ifdef _WIN32
#define EMBED_PYLIB_CACHE "libpython.dll"
#else
#ifdef __APPLE__
#define EMBED_PYLIB_CACHE "libpython.dylib"
#else
#define EMBED_PYLIB_CACHE "libpython.so"
#endif
#endif

/* 参数注入: [自身路径, 用户参数...] 逐项 base64 后逗号连接（env 无法携带 \0） */
static size_t b64_encode(const uint8_t *src, size_t n, char *out)
{
    static const char tab[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)src[i] << 16;
        int pad = (n - i) < 2 ? 1 : 0;
        if (i + 1 < n) v |= (uint32_t)src[i + 1] << 8;
        if (i + 2 < n) v |= src[i + 2];
        out[o++] = tab[(v >> 18) & 63];
        out[o++] = tab[(v >> 12) & 63];
        out[o++] = pad ? '=' : tab[(v >> 6) & 63];
        out[o++] = (n - i) < 3 ? '=' : tab[v & 63];
    }
    out[o] = 0;
    return o;
}

/* 解释器 -c 一行加载器: 从环境变量 PYTHUB_BOOT 读入引导器源码并 exec */
#define MEM_MINI_LOADER "import os;exec(os.environ['PYTHUB_BOOT'])"
/* TOC 内路径统一 POSIX 分隔符（校验用）；ENTRY_REL 是文件系统相对路径（平台分隔符） */
#ifdef _WIN32
#define ENTRY_REL_TOC "runtime/python.exe"
#else
#define ENTRY_REL_TOC "runtime/bin/python3"
#endif
#ifdef _WIN32
static const wchar_t ENTRY_REL[] = L"runtime\\python.exe";
#else
static const char ENTRY_REL[] = "runtime/bin/python3";
#endif

static const char MAGIC[8] = { 'P', 'Y', 'T', 'H', 'U', 'B', 'E', 'X' };

/* ---------------- 缓存清理（P2）公共定义 ----------------
 * 同应用旧 md5 目录 LRU 清理 + 缓存大小上限驱逐（默认 4GB），
 * 上限可用环境变量 PYTHUB_CACHE_MAX_MB（MB）覆盖。 */
#ifndef CACHE_MAX_BYTES
#define CACHE_MAX_BYTES ((uint64_t)4 << 30)
#endif

static uint64_t cache_cap_bytes(void)
{
    const char *env = getenv("PYTHUB_CACHE_MAX_MB");
    if (env && *env) {
        char *end = NULL;
        unsigned long long v = strtoull(env, &end, 10);
        if (end && *end == 0 && v > 0)
            return (uint64_t)v << 20;
    }
    return CACHE_MAX_BYTES;
}

/* cookie 中的应用名字段防御性净化（归档可能被构造）:
 * 仅保留 [A-Za-z0-9._-]，其余替换 '_'，空则 "_app"。 */
static void sanitize_app(char *app /* APP_FIELD_SIZE + 1 字节 */)
{
    for (int i = 0; app[i]; i++) {
        char c = app[i];
        int ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z')
                 || (c >= 'A' && c <= 'Z') || c == '.' || c == '-' || c == '_';
        if (!ok)
            app[i] = '_';
    }
    if (!app[0])
        memcpy(app, "_app", 4);
}

/* 运行状态输出（窗口子系统无控制台，静默） */
static void boot_log(const char *msg)
{
#ifndef GUI_APP
    puts(msg);
#else
    (void)msg;
#endif
}

static void die(const char *fmt, ...)
{
    char msg[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
#ifdef _WIN32
#ifdef GUI_APP
    /* 窗口子系统没有控制台，致命错误用消息框呈现 */
    int n = MultiByteToWideChar(CP_UTF8, 0, msg, -1, NULL, 0);
    wchar_t wmsg[1024];
    MultiByteToWideChar(CP_UTF8, 0, msg, -1, wmsg, n);
    MessageBoxW(NULL, wmsg, L"Pythub", MB_OK | MB_ICONERROR);
#else
    fprintf(stderr, "[bootex] %s\n", msg);
#endif
#else
    fprintf(stderr, "[bootex] %s\n", msg);
#endif
    exit(1);
}

static uint32_t rd32(const uint8_t *p)
{
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}

static uint64_t rd64(const uint8_t *p)
{
    uint64_t v;
    memcpy(&v, p, 8);
    return v;
}

/* 取出条目内容（按需 zlib 解压）。返回 malloc 缓冲（调用方负责 free），
 * 未压缩条目返回 NULL（调用方直接引用归档缓冲）。
 * 直接用 tinfl 解压 zlib 流（PARSE_ZLIB_HEADER 时 tinfl 自带 adler-32 校验）。 */
static uint8_t *entry_payload(const uint8_t *src, uint32_t stored, uint32_t raw, uint8_t flags)
{
    if (flags & ~((uint8_t)(FLAG_DEFLATE | FLAG_SYMLINK | FLAG_MEM)))
        die("未知归档条目标志: %u", flags);
    if (!(flags & FLAG_DEFLATE))
        return NULL;
    uint8_t *out = (uint8_t *)malloc(raw ? raw : 1);
    if (!out)
        die("内存不足");
    size_t n = tinfl_decompress_mem_to_mem(out, raw, src, stored,
                                           TINFL_FLAG_PARSE_ZLIB_HEADER |
                                               TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
    if (n == TINFL_DECOMPRESS_MEM_TO_MEM_FAILED || n != raw)
        die("条目解压失败，归档可能已损坏");
    return out;
}

/* 防路径穿越（zip-slip）: 归档可能被篡改，解压前校验每个条目路径。
 * 拒绝: 绝对路径、Windows 盘符、任何位置的 ".." 路径段。 */
static int unsafe_path(const char *path)
{
    size_t n = strlen(path);
    if (n == 0)
        return 1;
    if (path[0] == '/' || path[0] == '\\')
        return 1;
    if (n >= 2 && path[1] == ':'
        && ((path[0] >= 'A' && path[0] <= 'Z') || (path[0] >= 'a' && path[0] <= 'z')))
        return 1;
    for (size_t i = 0; i + 1 < n; i++) {
        if (path[i] == '.' && path[i + 1] == '.') {
            int start_ok = (i == 0) || path[i - 1] == '/' || path[i - 1] == '\\';
            char end = path[i + 2] == 0 ? 0 : path[i + 2];
            int end_ok = (end == '/' || end == '\\' || (i + 2 == n));
            if (start_ok && end_ok)
                return 1;
        }
    }
    return 0;
}

/* ---------------- MD5（公开域实现，标准 RFC 1321 算法）---------------- */
static const uint32_t MD5_K[64] = {
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee,
    0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be,
    0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa,
    0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed,
    0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c,
    0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05,
    0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039,
    0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1,
    0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391,
};
static const uint8_t MD5_S[64] = {
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
};

static void md5_transform(uint32_t st[4], const uint8_t block[64])
{
    uint32_t w[16];
    memcpy(w, block, 64); /* 目标平台均为小端 */
    uint32_t a = st[0], b = st[1], c = st[2], d = st[3];
    for (int i = 0; i < 64; i++) {
        uint32_t f, g;
        if (i < 16)      { f = (b & c) | (~b & d); g = i; }
        else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
        else if (i < 48) { f = b ^ c ^ d;          g = (3 * i + 5) % 16; }
        else             { f = c ^ (b | ~d);       g = (7 * i) % 16; }
        f += a + MD5_K[i] + w[g];
        uint32_t tmp = d;
        d = c; c = b;
        b = b + ((f << MD5_S[i]) | (f >> (32 - MD5_S[i])));
        a = tmp;
    }
    st[0] += a; st[1] += b; st[2] += c; st[3] += d;
}

static void md5_1(const void *data, size_t len, uint8_t out[16])
{
    static const uint32_t init[4] = { 0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476 };
    uint32_t st[4];
    memcpy(st, init, sizeof(st));
    const uint8_t *p = (const uint8_t *)data;
    size_t full = len & ~(size_t)63;
    for (size_t i = 0; i < full; i += 64)
        md5_transform(st, p + i);
    uint8_t tail[128];
    size_t rem = len - full;
    memcpy(tail, p + full, rem);
    tail[rem] = 0x80;
    size_t total = (rem + 1 <= 56) ? 64 : 128;
    memset(tail + rem + 1, 0, total - rem - 1);
    uint64_t bits = (uint64_t)len * 8;
    memcpy(tail + total - 8, &bits, 8);
    md5_transform(st, tail);
    if (total == 128)
        md5_transform(st, tail + 64);
    memcpy(out, st, 16);
}

static void md5_hex(const void *data, size_t len, char hex[33])
{
    uint8_t d[16];
    md5_1(data, len, d);
    static const char HEX[] = "0123456789abcdef";
    for (int i = 0; i < 16; i++) {
        hex[i * 2] = HEX[d[i] >> 4];
        hex[i * 2 + 1] = HEX[d[i] & 15];
    }
    hex[32] = 0;
}

/* ---------------- 归档/缓存完整性校验（P2） ---------------- */

/* 归档体完整性校验: (数据区+TOC) 的 MD5 必须与 cookie 记录一致，
 * 防止单文件本体被截断、篡改或拼接损坏。 */
static void verify_body(const uint8_t *buf, const char *md5hex,
                        uint64_t payload_offset, uint64_t toc_offset, uint64_t toc_size)
{
    if (toc_offset + toc_size < payload_offset || toc_offset + toc_size > SIZE_MAX)
        die("归档偏移非法");
    char got[33];
    md5_hex(buf + payload_offset, (size_t)(toc_offset + toc_size - payload_offset), got);
    if (memcmp(got, md5hex, 32) != 0)
        die("归档完整性校验失败，文件已被修改或损坏");
}

/* 在 TOC 中按路径查找条目（TOC 路径为 POSIX 分隔符）。找到返回 1。 */
static int toc_find(const uint8_t *buf, uint64_t toc_offset, const char *rel,
                    uint64_t *doff, uint32_t *stored, uint32_t *raw, uint8_t *flags)
{
    const uint8_t *p = buf + toc_offset;
    uint32_t count = rd32(p);
    p += 4;
    size_t n = strlen(rel);
    for (uint32_t i = 0; i < count; i++) {
        uint32_t plen = rd32(p); p += 4;
        uint32_t s = rd32(p); p += 4;
        uint32_t r = rd32(p); p += 4;
        uint64_t d = rd64(p); p += 8;
        uint8_t f = *p; p += 1;
        if (plen == n && memcmp(p, rel, n) == 0) {
            *doff = d; *stored = s; *raw = r; *flags = f;
            return 1;
        }
        p += plen;
    }
    return 0;
}

/* 取出 TOC 条目内容到内存（必要时 tinfl 解压），供 mem 模式使用。
 * 返回 malloc 缓冲或指向归档缓冲的指针（未压缩条目），调用方按 is_heap 决定是否 free。 */
static void mem_entry_load(const uint8_t *buf, uint64_t toc_offset, const char *rel,
                           uint8_t **out_data, size_t *out_len, int *out_heap)
{
    uint64_t doff;
    uint32_t stored, raw;
    uint8_t flags;
    if (!toc_find(buf, toc_offset, rel, &doff, &stored, &raw, &flags))
        die("归档缺少内存载荷条目: %s", rel);
    if (!(flags & FLAG_MEM))
        die("条目缺少内存载荷标志: %s", rel);
    uint8_t *infl = entry_payload(buf + doff, stored, raw, flags);
    if (infl) {
        *out_data = infl;
        *out_len = raw;
        *out_heap = 1;
    } else {
        *out_data = (uint8_t *)(buf + doff);
        *out_len = stored;
        *out_heap = 0;
    }
}

/* 归档内启动器条目的原始内容 MD5（必要时解压）。
 * Windows 启动器永远不是符号链接，直接用 MD5 比对即可；
 * POSIX 的符号链接感知校验见 launcher_cache_ok_n。 */
#ifdef _WIN32
static void entry_md5(const uint8_t *buf, uint64_t toc_offset, uint8_t digest[16])
{
    uint64_t doff;
    uint32_t stored, raw;
    uint8_t flags;
    if (!toc_find(buf, toc_offset, ENTRY_REL_TOC, &doff, &stored, &raw, &flags))
        die("归档缺少启动器条目: %s", ENTRY_REL_TOC);
    uint8_t *infl = entry_payload(buf + doff, stored, raw, flags);
    md5_1(infl ? infl : buf + doff, infl ? raw : stored, digest);
    free(infl);
}
#endif

#ifdef PYTHUB_STATIC
/* static 模式公共: 解释器内嵌进引导器，需要 Python.h 与 frozen 注册。
 * POSIX 与 Windows 的 spawn_static_* 共用 frozen 注册逻辑。 */
#include <Python.h>

/* 解析 frozen blob（打包期由 exe_archive 生成）:
 * [u32 count]{[u16 namelen][name][u8 is_pkg][u32 codelen][marshal]}
 * 直接引用归档缓冲区（零拷贝，进程存活期内有效），注册到
 * PyImport_FrozenModules（3.12 pre-init 时全局表为 NULL, 无需合并）。 */
static void bootex_register_frozen(const uint8_t *buf, uint64_t toc_offset)
{
    uint8_t *data;
    size_t len;
    int heap;
    mem_entry_load(buf, toc_offset, EMBED_STDLZF_REL, &data, &len, &heap);
    const uint8_t *p = data;
    const uint8_t *end = data + len;
    uint32_t count = rd32(p);
    p += 4;
    struct _frozen *tbl = calloc(count + 1, sizeof(struct _frozen));
    if (!tbl)
        die("frozen 表内存分配失败");
    for (uint32_t i = 0; i < count && p + 3 <= end; i++) {
        uint16_t nl = (uint16_t)(p[0] | (p[1] << 8));
        p += 2;
        if (p + nl + 9 > end)
            die("frozen blob 损坏");
        tbl[i].name = (const char *)p;
        p += nl;
        tbl[i].is_package = *p++;
        uint32_t cl = rd32(p);
        p += 4;
        if (p + cl > end)
            die("frozen blob 代码段越界");
        tbl[i].code = (const unsigned char *)p;
        p += cl;
        tbl[i].size = (int)cl;
#if PY_VERSION_HEX >= 0x030C0000
        tbl[i].get_code = NULL; /* 直接使用 code/size 内存数据 */
#endif
    }
    tbl[count].name = NULL;
    tbl[count].code = NULL;
    tbl[count].size = 0;
    tbl[count].get_code = NULL;
    PyImport_FrozenModules = tbl;
}
#endif

#ifdef _WIN32
/* ---------------------------------------------------------- Windows 分支 */

static wchar_t *utf8w(const char *s)
{
    int n = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    wchar_t *w = (wchar_t *)malloc((size_t)n * sizeof(wchar_t));
    if (!w)
        die("内存不足");
    MultiByteToWideChar(CP_UTF8, 0, s, -1, w, n);
    return w;
}

static uint8_t *read_self(size_t *out_len)
{
    wchar_t self[32768];
    if (!GetModuleFileNameW(NULL, self, 32768))
        die("无法获取自身路径");
    FILE *f = _wfopen(self, L"rb");
    if (!f)
        die("无法读取自身文件");
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    rewind(f);
    if (size <= 0)
        die("自身文件为空");
    uint8_t *buf = (uint8_t *)malloc((size_t)size);
    if (!buf)
        die("内存不足");
    if (fread(buf, 1, (size_t)size, f) != (size_t)size)
        die("读取不完整");
    fclose(f);
    *out_len = (size_t)size;
    return buf;
}

/* 逐段创建目录（忽略已存在的报错；需要临时改写分隔符，调用方传入可写缓冲） */
static void mkdir_parents_w(wchar_t *full)
{
    for (wchar_t *p = full; *p; p++) {
        if (*p == L'\\') {
            *p = 0;
            CreateDirectoryW(full, NULL); /* 已存在则失败，忽略 */
            *p = L'\\';
        }
    }
}

static void write_file_w(const wchar_t *full, const uint8_t *data, size_t size)
{
    FILE *f = _wfopen(full, L"wb");
    if (!f)
        die("写入文件失败");
    if (fwrite(data, 1, size, f) != size)
        die("写入不完整");
    fclose(f);
}

static int exists_w(const wchar_t *path)
{
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

static void extract_w(const uint8_t *buf, uint64_t toc_offset, const wchar_t *cache)
{
    const uint8_t *p = buf + toc_offset;
    uint32_t count = rd32(p);
    p += 4;
    wchar_t full[32768];
    for (uint32_t i = 0; i < count; i++) {
        uint32_t plen = rd32(p); p += 4;
        uint32_t stored = rd32(p); p += 4;
        uint32_t raw = rd32(p); p += 4;
        uint64_t doff = rd64(p); p += 8;
        uint8_t flags = *p; p += 1;
        char path[1024];
        if (plen >= sizeof(path))
            die("归档内路径过长");
        memcpy(path, p, plen);
        path[plen] = 0;
        p += plen;
        if (flags & FLAG_MEM)
            continue; /* 内存载荷条目: 不落盘，由 spawn_mem 直传解释器 */
        if (unsafe_path(path))
            die("归档条目包含不安全路径，已中止解压: %s", path);
        wchar_t *wpath = utf8w(path);
        for (wchar_t *q = wpath; *q; q++)
            if (*q == L'/')
                *q = L'\\';
        _snwprintf_s(full, 32768, _TRUNCATE, L"%ls\\%ls", cache, wpath);
        mkdir_parents_w(full);
        uint8_t *infl = entry_payload(buf + doff, stored, raw, flags);
        write_file_w(full, infl ? infl : buf + doff, infl ? raw : stored);
        free(infl);
        free(wpath);
    }
}

/* 递归删除目录树 */
static void rmrf_w(const wchar_t *path)
{
    wchar_t pattern[32768];
    _snwprintf_s(pattern, 32768, _TRUNCATE, L"%ls\\*", path);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) {
        DeleteFileW(path);
        RemoveDirectoryW(path);
        return;
    }
    do {
        if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0)
            continue;
        wchar_t child[32768];
        _snwprintf_s(child, 32768, _TRUNCATE, L"%ls\\%ls", path, fd.cFileName);
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            rmrf_w(child);
        else
            DeleteFileW(child);
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    RemoveDirectoryW(path);
}

/* ---------------- 缓存清理（P2, Windows 分支） ---------------- */

/* 刷新 LRU 时间戳（每次运行都写，保证"最近使用"判断准确） */
static void touch_stamp_w(const wchar_t *cache)
{
    wchar_t stamp[32768];
    _snwprintf_s(stamp, 32768, _TRUNCATE, L"%ls\\.stamp", cache);
    FILE *f = _wfopen(stamp, L"wb");
    if (f)
        fclose(f);
}

/* 目录占用大小（递归） */
static uint64_t du_w(const wchar_t *path)
{
    wchar_t pattern[32768];
    _snwprintf_s(pattern, 32768, _TRUNCATE, L"%ls\\*", path);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    uint64_t total = 0;
    do {
        if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0)
            continue;
        wchar_t child[32768];
        _snwprintf_s(child, 32768, _TRUNCATE, L"%ls\\%ls", path, fd.cFileName);
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            total += du_w(child);
        else
            total += ((uint64_t)fd.nFileSizeHigh << 32) | fd.nFileSizeLow;
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return total;
}

/* LRU 时间: 优先 .stamp 的修改时间，回退目录修改时间（FILETIME, 100ns） */
static uint64_t lru_time_w(const wchar_t *dir)
{
    wchar_t stamp[32768];
    _snwprintf_s(stamp, 32768, _TRUNCATE, L"%ls\\.stamp", dir);
    WIN32_FILE_ATTRIBUTE_DATA fa;
    if (GetFileAttributesExW(stamp, GetFileExInfoStandard, &fa))
        return ((uint64_t)fa.ftLastWriteTime.dwHighDateTime << 32)
               | fa.ftLastWriteTime.dwLowDateTime;
    if (GetFileAttributesExW(dir, GetFileExInfoStandard, &fa))
        return ((uint64_t)fa.ftLastWriteTime.dwHighDateTime << 32)
               | fa.ftLastWriteTime.dwLowDateTime;
    return 0;
}

static int hex32_w(const wchar_t *s)
{
    size_t n = wcslen(s);
    if (n != 32)
        return 0;
    for (size_t i = 0; i < n; i++) {
        wchar_t c = s[i];
        if (!((c >= L'0' && c <= L'9') || (c >= L'a' && c <= L'f')))
            return 0;
    }
    return 1;
}

/* 同应用旧 md5 目录清理: 删除应用组目录下除当前版本外的所有 md5 目录、
 * 残留 .part 临时目录（当前版本的除外）。尽力而为，失败忽略。 */
static void cleanup_app_w(const wchar_t *appdir, const wchar_t *cur_md5)
{
    wchar_t pattern[32768];
    _snwprintf_s(pattern, 32768, _TRUNCATE, L"%ls\\*", appdir);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE)
        return;
    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            size_t n = wcslen(fd.cFileName);
            int is_md5 = hex32_w(fd.cFileName);
            int stale_part = n > 5 && wcscmp(fd.cFileName + n - 5, L".part") == 0;
            int cur_part = n == 37 && wcsncmp(fd.cFileName, cur_md5, 32) == 0;
            if (((is_md5 && wcscmp(fd.cFileName, cur_md5) != 0) || stale_part)
                && !cur_part) {
                wchar_t child[32768];
                _snwprintf_s(child, 32768, _TRUNCATE, L"%ls\\%ls", appdir, fd.cFileName);
                rmrf_w(child);
            }
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);
}

/* 缓存大小上限: exe 缓存总占用超限时，从最久未用的应用组开始驱逐
 * （永不删除当前应用组）。应用组列表动态分配，数量无硬编码上限。
 * 尽力而为，失败忽略。 */
static void enforce_cap_w(const wchar_t *exe_root, const wchar_t *cur_app)
{
    wchar_t pattern[32768];
    _snwprintf_s(pattern, 32768, _TRUNCATE, L"%ls\\*", exe_root);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE)
        return;
    size_t capacity = 64;
    wchar_t (*names)[64] = malloc(capacity * sizeof(*names));
    uint64_t *sizes = malloc(capacity * sizeof(*sizes));
    uint64_t *times = malloc(capacity * sizeof(*times));
    if (!names || !sizes || !times) {
        free(names);
        free(sizes);
        free(times);
        FindClose(h);
        return;
    }
    int n = 0;
    uint64_t total = 0;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
            continue;
        if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0)
            continue;
        if ((size_t)n >= capacity) {
            size_t ncap = capacity * 2;
            wchar_t (*nn)[64] = realloc(names, ncap * sizeof(*nn));
            uint64_t *ns = realloc(sizes, ncap * sizeof(*ns));
            uint64_t *nt = realloc(times, ncap * sizeof(*nt));
            if (nn)
                names = nn;
            if (ns)
                sizes = ns;
            if (nt)
                times = nt;
            if (!nn || !ns || !nt)
                break; /* 内存不足: 用已收集的部分尽力清理 */
            capacity = ncap;
        }
        wchar_t child[32768];
        _snwprintf_s(child, 32768, _TRUNCATE, L"%ls\\%ls", exe_root, fd.cFileName);
        _snwprintf_s(names[n], 64, _TRUNCATE, L"%ls", fd.cFileName);
        sizes[n] = du_w(child);
        times[n] = lru_time_w(child);
        total += sizes[n];
        n++;
    } while (FindNextFileW(h, &fd));
    FindClose(h);

    uint64_t cap = cache_cap_bytes();
    while (total > cap) {
        int victim = -1;
        for (int i = 0; i < n; i++) {
            if (sizes[i] == 0 || wcscmp(names[i], cur_app) == 0)
                continue;
            if (victim < 0 || times[i] < times[victim])
                victim = i;
        }
        if (victim < 0)
            break;
        wchar_t child[32768];
        _snwprintf_s(child, 32768, _TRUNCATE, L"%ls\\%ls", exe_root, names[victim]);
        rmrf_w(child);
        total -= sizes[victim];
        sizes[victim] = 0;
    }
    free(names);
    free(sizes);
    free(times);
}

/* 进程级互斥（Windows 命名互斥体），保证并发首解压串行 */
static HANDLE g_lock = NULL;

static void lock_acquire_w(const char *app, const char *md5)
{
    wchar_t name[128];
    wchar_t *wapp = utf8w(app);
    wchar_t *wmd5 = utf8w(md5);
    _snwprintf_s(name, 128, _TRUNCATE, L"Local\\pythub-exe-%ls-%ls", wapp, wmd5);
    free(wapp);
    free(wmd5);
    g_lock = CreateMutexW(NULL, FALSE, name);
    if (g_lock)
        WaitForSingleObject(g_lock, INFINITE);
}

static void lock_release_w(void)
{
    if (g_lock) {
        ReleaseMutex(g_lock);
        CloseHandle(g_lock);
        g_lock = NULL;
    }
}

/* 原子解压: 解压到 <缓存>.part，校验启动器后原子改名就位 */
static void extract_atomic_w(const uint8_t *buf, uint64_t toc_offset, const wchar_t *cache)
{
    wchar_t part[32768];
    wchar_t entry[32768];
    _snwprintf_s(part, 32768, _TRUNCATE, L"%ls.part", cache);
    if (exists_w(part))
        rmrf_w(part); /* 清理上次中断留下的临时目录 */
    extract_w(buf, toc_offset, part);
    _snwprintf_s(entry, 32768, _TRUNCATE, L"%ls\\%ls", part, ENTRY_REL);
    if (!exists_w(entry)) {
        rmrf_w(part);
        die("解压失败：启动器未生成，归档可能已损坏");
    }
    if (exists_w(cache))
        rmrf_w(cache); /* 清理异常残留（完整缓存不存在但目录在） */
    if (!MoveFileExW(part, cache, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        rmrf_w(part);
        die("解压目录就位失败 (GetLastError=%lu)", GetLastError());
    }
}

/* 缓存启动器完整性校验: 与归档内启动器条目 MD5 比对，防缓存目录被篡改 */
static int launcher_md5_ok_w(const wchar_t *cache, const uint8_t expect[16])
{
    wchar_t full[32768];
    _snwprintf_s(full, 32768, _TRUNCATE, L"%ls\\%ls", cache, ENTRY_REL);
    FILE *f = _wfopen(full, L"rb");
    if (!f)
        return 0;
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    rewind(f);
    if (size <= 0) {
        fclose(f);
        return 0;
    }
    uint8_t *data = (uint8_t *)malloc((size_t)size);
    if (!data) {
        fclose(f);
        die("内存不足");
    }
    if (fread(data, 1, (size_t)size, f) != (size_t)size) {
        fclose(f);
        free(data);
        return 0;
    }
    fclose(f);
    uint8_t got[16];
    md5_1(data, (size_t)size, got);
    free(data);
    return memcmp(got, expect, 16) == 0;
}

static int spawn_w(const wchar_t *cache, int argc, char **argv)
{
    wchar_t cmd[65536];
    int pos = _snwprintf_s(cmd, 65536, _TRUNCATE, L"\"%ls\\%ls\" \"%ls\\%hs\"",
                         cache, ENTRY_REL, cache, PYZ_REL);
    if (pos < 0 || pos >= 65536)
        die("命令行过长");
    int nArgs = 0;
    wchar_t **cl = CommandLineToArgvW(GetCommandLineW(), &nArgs);
    if (!cl)
        die("命令行解析失败");
    for (int i = 1; i < nArgs && pos < 65530; i++) {
        int w = _snwprintf_s(cmd + pos, 65536 - pos, _TRUNCATE, L" \"%ls\"", cl[i]);
        if (w < 0)
            break;
        pos += w;
    }
    LocalFree(cl);
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    memset(&pi, 0, sizeof(pi));
    si.cb = sizeof(si);
    /* GUI 模式: 本程序是窗口子系统（无控制台），但 python.exe 是控制台
     * 子系统程序，不加 CREATE_NO_WINDOW 会为它新开一个可见控制台黑框 */
#ifdef GUI_APP
    const DWORD cflags = CREATE_NO_WINDOW;
#else
    const DWORD cflags = 0;
#endif
    if (!CreateProcessW(NULL, cmd, NULL, NULL, TRUE, cflags, NULL, cache, &si, &pi))
        die("无法启动应用 (GetLastError=%lu)", GetLastError());
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (int)code;
}

/* mem 模式启动（Windows）: 引导器源码经 PYTHUB_BOOT 环境变量注入，
 * 应用字节码经可继承管道句柄（值经命令行传入）直传解释器，全程内存。 */
static int spawn_mem_w(const wchar_t *cache, const uint8_t *buf, uint64_t toc_offset)
{
    uint8_t *boot, *payload;
    size_t bootlen, paylen;
    int boot_heap, pay_heap;
    mem_entry_load(buf, toc_offset, MEM_BOOT_REL, &boot, &bootlen, &boot_heap);
    mem_entry_load(buf, toc_offset, MEM_PAYLOAD_REL, &payload, &paylen, &pay_heap);

    /* 引导器源码注入环境变量（CreateProcess 不传环境块 -> 子进程继承） */
    char *bootz = (char *)malloc(bootlen + 1);
    if (!bootz)
        die("内存不足");
    memcpy(bootz, boot, bootlen);
    bootz[bootlen] = 0;
    wchar_t *wboot = utf8w(bootz);
    if (!SetEnvironmentVariableW(L"PYTHUB_BOOT", wboot))
        die("无法设置引导器环境变量");
    free(wboot);
    free(bootz);
    if (boot_heap)
        free(boot);

    /* 管道: 读端可继承，句柄值经命令行传给子进程 */
    SECURITY_ATTRIBUTES sa;
    memset(&sa, 0, sizeof(sa));
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    HANDLE rh = NULL, wh = NULL;
    if (!CreatePipe(&rh, &wh, &sa, 0))
        die("无法创建载荷管道");
    SetHandleInformation(rh, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);

    /* 命令行: "<python>" -I -c "<mini loader>" <handle> "<self>" <args...>
     * 子进程内 sys.argv = ['-c', <handle>, <self>, args...] */
    wchar_t self[32768];
    if (!GetModuleFileNameW(NULL, self, 32768))
        die("无法获取自身路径");
    wchar_t cmd[65536];
    int pos = _snwprintf_s(cmd, 65536, _TRUNCATE,
                           L"\"%ls\\%ls\" -I -c \"%hs\" %I64u \"%ls\"",
                           cache, ENTRY_REL, MEM_MINI_LOADER,
                           (unsigned long long)(uintptr_t)rh, self);
    if (pos < 0 || pos >= 65536)
        die("命令行过长");
    int nArgs = 0;
    wchar_t **cl = CommandLineToArgvW(GetCommandLineW(), &nArgs);
    if (!cl)
        die("命令行解析失败");
    for (int i = 1; i < nArgs && pos < 65530; i++) {
        int w = _snwprintf_s(cmd + pos, 65536 - pos, _TRUNCATE, L" \"%ls\"", cl[i]);
        if (w < 0)
            break;
        pos += w;
    }
    LocalFree(cl);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    memset(&pi, 0, sizeof(pi));
    si.cb = sizeof(si);
#ifdef GUI_APP
    const DWORD cflags = CREATE_NO_WINDOW;
#else
    const DWORD cflags = 0;
#endif
    /* 工作目录不传（保持调用方 cwd），载荷经由句柄注入而非落盘 */
    if (!CreateProcessW(NULL, cmd, NULL, NULL, TRUE, cflags, NULL, NULL, &si, &pi))
        die("无法启动应用 (GetLastError=%lu)", GetLastError());
    CloseHandle(rh); /* 父进程读端已无用（子进程持有继承副本） */

    /* 字节码写入管道（子进程从 fd 读入内存，不落盘） */
    DWORD written = 0;
    size_t off = 0;
    while (off < paylen) {
        if (!WriteFile(wh, payload + off, (DWORD)(paylen - off), &written, NULL) || written == 0)
            break; /* 子进程提前退出等情况: 尽力而为，退出码以子进程为准 */
        off += written;
    }
    CloseHandle(wh);

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    if (pay_heap)
        free(payload);
    return (int)code;
}

/* embed 内嵌解释器模式（需求 64, Windows）: 引导器进程内 LoadLibrary
 * libpython + GetProcAddress 初始化解释器并执行引导器源码。
 * 应用结束经 SystemExit -> os._exit 直接退出本进程（退出码即应用退出码）。 */
static int spawn_embed_w(const wchar_t *cache, const uint8_t *buf,
                         uint64_t toc_offset)
{
    /* 解出 libpython 与 stdlib.zip 到缓存（仅两个文件） */
    uint8_t *data;
    size_t dlen;
    int heap;
    wchar_t pylib[32768], stdlz[32768];
    /* 确保缓存目录存在（常规路径由 extract_atomic_w 创建，embed 跳过了） */
    {
        wchar_t parent[32768];
        _snwprintf_s(parent, 32768, _TRUNCATE, L"%ls", cache);
        wchar_t *slash = wcsrchr(parent, L'\\');
        if (slash) {
            *slash = 0;
            CreateDirectoryW(parent, NULL); /* <exe_root>\<app> */
        }
        CreateDirectoryW(cache, NULL); /* <exe_root>\<app>\<md5> */
    }
    mem_entry_load(buf, toc_offset, EMBED_PYLIB_REL, &data, &dlen, &heap);
    _snwprintf_s(pylib, 32768, _TRUNCATE, L"%ls\\%ls", cache, L"" EMBED_PYLIB_CACHE);
    write_file_w(pylib, data, dlen);
    if (heap)
        free(data);
    mem_entry_load(buf, toc_offset, EMBED_STDLZ_REL, &data, &dlen, &heap);
    _snwprintf_s(stdlz, 32768, _TRUNCATE, L"%ls\\stdlib.zip", cache);
    write_file_w(stdlz, data, dlen);
    if (heap)
        free(data);

    /* Tcl/Tk 数据 zip（可选常规条目）: 落盘到缓存，runner 释放真实文件目录 */
    {
        uint64_t toff;
        uint32_t tst, trw;
        uint8_t tfl;
        if (toc_find(buf, toc_offset, EMBED_TCLTK_REL, &toff, &tst, &trw, &tfl)) {
            uint8_t *tk = entry_payload(buf + toff, tst, trw, tfl);
            const uint8_t *tksrc = tk ? tk : (buf + toff);
            size_t tklen = tk ? trw : tst;
            wchar_t wtkp[32768];
            wchar_t wrel[64];
            MultiByteToWideChar(CP_UTF8, 0, EMBED_TCLTK_REL, -1, wrel, 64);
            _snwprintf_s(wtkp, 32768, _TRUNCATE, L"%ls\\%ls", cache, wrel);
            write_file_w(wtkp, tksrc, tklen);
            if (tk)
                free(tk);
        }
    }

    /* 环境变量注入（runner 内 pop）: PYTHONPATH 交给 zipimport；argv 逐项
     * base64 逗号连接（首项为自身路径，与 mem 模式 sys.argv 语义对齐） */
    wchar_t wstdlz[32768];
    wchar_t wname[64];
    MultiByteToWideChar(CP_UTF8, 0, EMBED_PYLIB_CACHE, -1, wname, 64);
    _snwprintf_s(wstdlz, 32768, _TRUNCATE, L"%ls\\%ls", cache, wname);
    (void)wstdlz;
    wchar_t wzip[32768];
    _snwprintf_s(wzip, 32768, _TRUNCATE, L"%ls\\stdlib.zip", cache);
    SetEnvironmentVariableW(L"PYTHONPATH", wzip);
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");

    wchar_t self[32768];
    if (!GetModuleFileNameW(NULL, self, 32768))
        die("无法获取自身路径");
    SetEnvironmentVariableW(L"PYTHUB_SELF", self);
    SetEnvironmentVariableW(L"PYTHUB_EXE_CACHE", cache); /* 缓存目录（runner 释放 tcl/tk 用） */

    char b64[65536];
    size_t bo = 0;
    int nArgs = 0;
    wchar_t **cl = CommandLineToArgvW(GetCommandLineW(), &nArgs);
    if (!cl)
        die("命令行解析失败");
    for (int i = 0; i < nArgs && bo + 8 < sizeof(b64); i++) {
        int utf8len = WideCharToMultiByte(CP_UTF8, 0, cl[i], -1,
                                          NULL, 0, NULL, NULL);
        char *item = (char *)malloc((size_t)utf8len);
        if (!item)
            die("内存不足");
        WideCharToMultiByte(CP_UTF8, 0, cl[i], -1, item, utf8len, NULL, NULL);
        size_t itemlen = strlen(item);
        if (i > 0)
            b64[bo++] = ',';
        bo += b64_encode((const uint8_t *)item, itemlen, b64 + bo);
        free(item);
    }
    LocalFree(cl);
    b64[bo] = 0;
    wchar_t *warg = utf8w(b64);
    SetEnvironmentVariableW(L"PYTHUB_EMBED_ARGV", warg);
    free(warg);

    /* 进程内加载解释器 */
    HMODULE h = LoadLibraryW(pylib);
    if (!h)
        die("无法加载 libpython (GetLastError=%lu)", GetLastError());
    void (*py_init)(void) = (void (*)(void))GetProcAddress(h, "Py_Initialize");
    int (*py_run)(const char *) =
        (int (*)(const char *))GetProcAddress(h, "PyRun_SimpleString");
    if (!py_init || !py_run)
        die("libpython 缺少嵌入式初始化符号");

    uint8_t *boot;
    size_t bootlen;
    int boot_heap;
    mem_entry_load(buf, toc_offset, MEM_BOOT_REL, &boot, &bootlen, &boot_heap);
    char *bootz = (char *)malloc(bootlen + 1);
    if (!bootz)
        die("内存不足");
    memcpy(bootz, boot, bootlen);
    bootz[bootlen] = 0;

    py_init();
    int r = py_run(bootz); /* runner 以 os._exit 收尾，正常不会到达这里 */
    if (boot_heap)
        free(boot);
    free(bootz);
    return r == 0 ? 0 : 1;
}

#ifdef PYTHUB_STATIC
/* static 内存模式（需求 64 第四档, Windows）: 解释器核心（PBS build/core
 * objs）与扩展模块 objs 静态链接进本二进制，stdlib frozen 注册——除
 * 第三方依赖 DLL（openssl/sqlite3, 经 app.wdlls 携带）外零落盘。
 * 扩展模块 init 由 pythub_extinit.c（打包期生成）注册 PyImport_Inittab。 */
extern void pythub_register_extensions(void);

static int spawn_static_w(const wchar_t *cache, const uint8_t *buf,
                          uint64_t toc_offset)
{
    /* 缓存目录（mkdir_parents_w 处理父目录） */
    {
        wchar_t parent[32768];
        _snwprintf_s(parent, 32768, _TRUNCATE, L"%ls", cache);
        wchar_t *slash = wcsrchr(parent, L'\\');
        if (slash) {
            *slash = 0;
            mkdir_parents_w(parent);
        }
        CreateDirectoryW(cache, NULL);
    }

    /* 自身路径（unicode 安全） */
    wchar_t selfw[32768];
    if (!GetModuleFileNameW(NULL, selfw, 32768))
        die("无法定位自身路径");
    int su8len = WideCharToMultiByte(CP_UTF8, 0, selfw, -1, NULL, 0, NULL, NULL);
    char *selfu8 = (char *)malloc((size_t)su8len);
    if (!selfu8)
        die("内存不足");
    WideCharToMultiByte(CP_UTF8, 0, selfw, -1, selfu8, su8len, NULL, NULL);

    /* app.wdlls zip（第三方依赖 DLL）+ app.tcltk zip: 落盘到缓存，
     * runner 解压（wdlls → 缓存根, DLL 经 PATH 前置的缓存目录搜索加载） */
    {
        const char *zip_rels[] = {APP_WDLLS_REL, EMBED_TCLTK_REL};
        for (int zi = 0; zi < 2; zi++) {
            uint64_t toff;
            uint32_t tst, trw;
            uint8_t tfl;
            if (!toc_find(buf, toc_offset, zip_rels[zi], &toff, &tst, &trw, &tfl))
                continue;
            uint8_t *zp = entry_payload(buf + toff, tst, trw, tfl);
            const uint8_t *zsrc = zp ? zp : (buf + toff);
            size_t zlen = zp ? trw : tst;
            wchar_t *zpw = utf8w(zip_rels[zi]);
            if (!zpw)
                die("内存不足");
            wchar_t zpath[32768];
            _snwprintf_s(zpath, 32768, _TRUNCATE, L"%ls\\%ls", cache, zpw);
            free(zpw);
            write_file_w(zpath, zsrc, zlen);
            if (zp)
                free(zp);
        }
    }

    /* PATH 前置缓存目录: 依赖 DLL（libcrypto-3-x64.dll 等）由 runner 解压
     * 到缓存后，_ssl/_sqlite3 等静态扩展在 import 期经 PATH 搜索加载 */
    {
        wchar_t old[32768];
        wchar_t newp[65536];
        DWORD n = GetEnvironmentVariableW(L"PATH", old, 32768);
        if (n > 0 && n < 32768)
            _snwprintf_s(newp, 65536, _TRUNCATE, L"%ls;%ls", cache, old);
        else
            _snwprintf_s(newp, 65536, _TRUNCATE, L"%ls", cache);
        SetEnvironmentVariableW(L"PATH", newp);
    }

    /* stdlib frozen 字节码注册（公共逻辑） */
    bootex_register_frozen(buf, toc_offset);

    /* 扩展模块注册（_ssl/_socket/pyexpat 等, 打包期生成表） */
    pythub_register_extensions();

    /* 环境变量注入 */
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
    {
        wchar_t *v = utf8w(selfu8);
        if (!v)
            die("内存不足");
        SetEnvironmentVariableW(L"PYTHUB_SELF", v);
        free(v);
    }
    SetEnvironmentVariableW(L"PYTHUB_EXE_CACHE", cache);
    /* argv: GetCommandLineW → utf8 → base64 逗号连接（unicode 参数安全） */
    {
        int n;
        wchar_t **cl = CommandLineToArgvW(GetCommandLineW(), &n);
        if (cl) {
            size_t cap = (size_t)(n > 0 ? n : 1) * 4 * 32768 + 16;
            char *b64 = (char *)malloc(cap);
            if (!b64)
                die("内存不足");
            size_t bo = 0;
            for (int i = 0; i < n; i++) {
                int u8len = WideCharToMultiByte(CP_UTF8, 0, cl[i], -1, NULL,
                                                0, NULL, NULL);
                char *item = (char *)malloc((size_t)u8len);
                if (!item)
                    die("内存不足");
                WideCharToMultiByte(CP_UTF8, 0, cl[i], -1, item, u8len,
                                    NULL, NULL);
                if (i > 0)
                    b64[bo++] = ',';
                bo += b64_encode((const uint8_t *)item, strlen(item), b64 + bo);
                free(item);
            }
            LocalFree(cl);
            b64[bo] = 0;
            wchar_t *warg = utf8w(b64);
            if (!warg)
                die("内存不足");
            SetEnvironmentVariableW(L"PYTHUB_EMBED_ARGV", warg);
            free(warg);
            free(b64);
        }
    }
    free(selfu8);

    uint8_t *boot;
    size_t bootlen;
    int boot_heap;
    mem_entry_load(buf, toc_offset, MEM_BOOT_REL, &boot, &bootlen, &boot_heap);
    char *bootz = (char *)malloc(bootlen + 1);
    if (!bootz)
        die("内存不足");
    memcpy(bootz, boot, bootlen);
    bootz[bootlen] = 0;

    Py_Initialize(); /* 解释器就在本二进制内——链接期符号，直接调用 */
    int r = PyRun_SimpleString(bootz); /* runner 以 os._exit 收尾 */
    if (boot_heap)
        free(boot);
    free(bootz);
    return r == 0 ? 0 : 1;
}
#endif /* PYTHUB_STATIC (Windows) */

#if defined(GUI_APP) && defined(_WIN32)
int main(int argc, char **argv);
/* -mwindows 使 PE 入口为 WinMainCRTStartup，需提供 WinMain 符号。
 * 引导器的参数实际通过 GetCommandLineW/CommandLineToArgvW 获取，
 * Windows 分支不依赖 main 的 argc/argv，这里直接转发即可。 */
int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    (void)inst; (void)prev; (void)cmd; (void)show;
    return main(0, NULL);
}
#endif

int main(int argc, char **argv)
{
    size_t len;
    uint8_t *buf = read_self(&len);
    if (len < COOKIE_SIZE)
        die("文件过小，归档不完整");
    const uint8_t *tail = buf + len - COOKIE_SIZE;
    if (memcmp(tail, MAGIC, 8) != 0)
        die("cookie 校验失败，不是 Pythub 自研单文件归档");
    uint64_t payload_offset = rd64(tail + 8);
    uint64_t toc_offset = rd64(tail + 16);
    uint64_t toc_size = rd64(tail + 24);
    char md5[33];
    memcpy(md5, tail + 32, 32);
    md5[32] = 0;
    char app[APP_FIELD_SIZE + 1];
    memcpy(app, tail + 64, APP_FIELD_SIZE);
    app[APP_FIELD_SIZE] = 0;
    sanitize_app(app);

    verify_body(buf, md5, payload_offset, toc_offset, toc_size);
    uint8_t expect[16];
    entry_md5(buf, toc_offset, expect);

    const wchar_t *base = _wgetenv(L"LOCALAPPDATA");
    if (!base || !*base)
        base = _wgetenv(L"TEMP");
    if (!base || !*base)
        base = L".";
    wchar_t *wmd5 = utf8w(md5);
    wchar_t *wapp = utf8w(app);
    wchar_t exe_root[32768];
    wchar_t appdir[32768];
    wchar_t cache[32768];
    _snwprintf_s(exe_root, 32768, _TRUNCATE, L"%ls\\pythub\\exe", base);
    _snwprintf_s(appdir, 32768, _TRUNCATE, L"%ls\\%ls", exe_root, wapp);
    _snwprintf_s(cache, 32768, _TRUNCATE, L"%ls\\%ls", appdir, wmd5);

    /* static/embed/mem 模式检测（需求 23/64）: 必须在常规解压之前——
     * mem/embed/static 归档无 ENTRY 常规条目（载荷全部 FLAG_MEM；embed 的
     * tcl/tk 数据条目由 spawn_embed 自行落盘），走到 extract 会误报"启动器
     * 未生成"。static 归档含 stdlzf 条目（frozen blob），优先于 mem 分发 */
    {
        uint64_t mdoff;
        uint32_t mst, mrw;
        uint8_t mfl;
#ifdef PYTHUB_STATIC
        if (toc_find(buf, toc_offset, EMBED_STDLZF_REL, &mdoff, &mst, &mrw, &mfl)) {
            int rc = spawn_static_w(cache, buf, toc_offset);
            free(buf);
            return rc;
        }
#endif
        int embed_mode = toc_find(buf, toc_offset, EMBED_PYLIB_REL, &mdoff, &mst, &mrw, &mfl);
        int mem_mode = toc_find(buf, toc_offset, MEM_BOOT_REL, &mdoff, &mst, &mrw, &mfl);
        if (embed_mode || mem_mode) {
            int rc = embed_mode ? spawn_embed_w(cache, buf, toc_offset)
                                : spawn_mem_w(cache, buf, toc_offset);
            free(buf);
            return rc;
        }
    }

    wchar_t entry[32768];
    _snwprintf_s(entry, 32768, _TRUNCATE, L"%ls\\%ls", cache, ENTRY_REL);
    int re_extract = 0;
    if (!exists_w(entry)) {
        boot_log("首次运行，正在解压...");
        re_extract = 1;
    } else if (!launcher_md5_ok_w(cache, expect)) {
        boot_log("缓存完整性校验失败，正在重新解压...");
        re_extract = 1;
    }
    if (re_extract) {
        lock_acquire_w(app, md5);
        /* 双重检查：并发实例可能已完成/修复解压 */
        if (!exists_w(entry) || !launcher_md5_ok_w(cache, expect))
            extract_atomic_w(buf, toc_offset, cache);
        lock_release_w();
        if (!exists_w(entry))
            die("解压失败：启动器未生成，归档可能已损坏");
    }
    /* P2 缓存清理: 每次运行刷新 LRU 时间戳；新版本首解压后清理同应用旧
     * md5 目录，并按缓存总大小上限驱逐最久未用的应用组（尽力而为） */
    touch_stamp_w(cache);
    if (re_extract) {
        cleanup_app_w(appdir, wmd5);
        enforce_cap_w(exe_root, wapp);
    }
    free(wapp);
    free(wmd5);
    /* 常规解压执行（mem/embed 已在上方提前分发） */
    {
        int rc = spawn_w(cache, argc, argv);
        free(buf);
        return rc;
    }
}

#else
/* ---------------------------------------------------------- POSIX 分支 */

static uint8_t *read_self(const char *argv0, size_t *out_len)
{
    char self[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", self, sizeof(self) - 1);
    if (n > 0)
        self[n] = 0;
    else if (!realpath(argv0, self))
        die("无法定位自身路径");
    FILE *f = fopen(self, "rb");
    if (!f)
        die("无法读取自身文件");
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    rewind(f);
    if (size <= 0)
        die("自身文件为空");
    uint8_t *buf = (uint8_t *)malloc((size_t)size);
    if (!buf)
        die("内存不足");
    if (fread(buf, 1, (size_t)size, f) != (size_t)size)
        die("读取不完整");
    fclose(f);
    *out_len = (size_t)size;
    return buf;
}

/* 逐段创建目录（忽略已存在的报错） */
static void mkdir_parents_n(char *full)
{
    for (char *p = full + 1; *p; p++) {
        if (*p == '/') {
            *p = 0;
            mkdir(full, 0777); /* 已存在则失败，忽略 */
            *p = '/';
        }
    }
}

static void write_file_n(const char *full, const uint8_t *data, size_t size)
{
    FILE *f = fopen(full, "wb");
    if (!f)
        die("写入文件失败: %s", full);
    if (fwrite(data, 1, size, f) != size)
        die("写入不完整");
    fclose(f);
    chmod(full, 0755);
}

static void extract_n(const uint8_t *buf, uint64_t toc_offset, const char *cache)
{
    const uint8_t *p = buf + toc_offset;
    uint32_t count = rd32(p);
    p += 4;
    char full[PATH_MAX * 2];
    for (uint32_t i = 0; i < count; i++) {
        uint32_t plen = rd32(p); p += 4;
        uint32_t stored = rd32(p); p += 4;
        uint32_t raw = rd32(p); p += 4;
        uint64_t doff = rd64(p); p += 8;
        uint8_t flags = *p; p += 1;
        char path[1024];
        if (plen >= sizeof(path))
            die("归档内路径过长");
        memcpy(path, p, plen);
        path[plen] = 0;
        p += plen;
        if (flags & FLAG_MEM)
            continue; /* 内存载荷条目: 不落盘，由 spawn_mem 直传解释器 */
        if (unsafe_path(path))
            die("归档条目包含不安全路径，已中止解压: %s", path);
        snprintf(full, sizeof(full), "%s/%s", cache, path);
        mkdir_parents_n(full);
        uint8_t *infl = entry_payload(buf + doff, stored, raw, flags);
        if (flags & FLAG_SYMLINK) {
            /* 符号链接条目: 内容为目标串。重建 symlink 而非复制目标文件
             * （bin/python3 -> python3.12 等，避免归档体积膨胀 ~15MB）。 */
            const uint8_t *data = infl ? infl : buf + doff;
            size_t dlen = infl ? raw : stored;
            char tgt[1024];
            if (dlen >= sizeof(tgt))
                die("符号链接目标过长: %s", path);
            memcpy(tgt, data, dlen);
            tgt[dlen] = 0;
            if (unsafe_path(tgt))
                die("归档条目包含不安全符号链接目标: %s -> %s", path, tgt);
            unlink(full);
            if (symlink(tgt, full) != 0)
                die("创建符号链接失败: %s -> %s", full, tgt);
            free(infl);
            continue;
        }
        write_file_n(full, infl ? infl : buf + doff, infl ? raw : stored);
        free(infl);
    }
}

/* 递归删除目录树 */
static void rmrf_n(const char *path)
{
    DIR *d = opendir(path);
    if (!d) {
        unlink(path);
        return;
    }
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
            continue;
        char child[PATH_MAX * 2];
        snprintf(child, sizeof(child), "%s/%s", path, e->d_name);
        struct stat st;
        if (lstat(child, &st) == 0 && S_ISDIR(st.st_mode))
            rmrf_n(child);
        else
            unlink(child);
    }
    closedir(d);
    rmdir(path);
}

/* ---------------- 缓存清理（P2, POSIX 分支） ---------------- */

/* 刷新 LRU 时间戳（每次运行都写，保证"最近使用"判断准确） */
static void touch_stamp_n(const char *cache)
{
    char stamp[PATH_MAX * 2];
    snprintf(stamp, sizeof(stamp), "%s/.stamp", cache);
    FILE *f = fopen(stamp, "wb");
    if (f)
        fclose(f);
}

/* 目录占用大小（递归） */
static uint64_t du_n(const char *path)
{
    DIR *d = opendir(path);
    if (!d)
        return 0;
    uint64_t total = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
            continue;
        char child[PATH_MAX * 2];
        snprintf(child, sizeof(child), "%s/%s", path, e->d_name);
        struct stat st;
        if (lstat(child, &st) == 0) {
            if (S_ISDIR(st.st_mode))
                total += du_n(child);
            else
                total += (uint64_t)st.st_size;
        }
    }
    closedir(d);
    return total;
}

/* LRU 时间: 优先 .stamp 的修改时间，回退目录修改时间（秒） */
static uint64_t lru_time_n(const char *dir)
{
    char stamp[PATH_MAX * 2];
    snprintf(stamp, sizeof(stamp), "%s/.stamp", dir);
    struct stat st;
    if (stat(stamp, &st) == 0)
        return (uint64_t)st.st_mtime;
    if (stat(dir, &st) == 0)
        return (uint64_t)st.st_mtime;
    return 0;
}

static int hex32_n(const char *s)
{
    size_t n = strlen(s);
    if (n != 32)
        return 0;
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            return 0;
    }
    return 1;
}

/* 同应用旧 md5 目录清理: 删除应用组目录下除当前版本外的所有 md5 目录、
 * 残留 .part 临时目录（当前版本的除外）与旧版本锁文件。尽力而为，失败忽略。 */
static void cleanup_app_n(const char *appdir, const char *cur_md5)
{
    DIR *d = opendir(appdir);
    if (!d)
        return;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        size_t n = strlen(e->d_name);
        int is_md5 = hex32_n(e->d_name);
        int stale_part = n > 5 && strcmp(e->d_name + n - 5, ".part") == 0;
        int cur_part = n == 37 && strncmp(e->d_name, cur_md5, 32) == 0;
        int old_lock = n == 37 && memcmp(e->d_name + 32, ".lock", 5) == 0
                       && memcmp(e->d_name, cur_md5, 32) != 0;
        if (((is_md5 && strcmp(e->d_name, cur_md5) != 0) || stale_part) && !cur_part) {
            char child[PATH_MAX * 2];
            snprintf(child, sizeof(child), "%s/%s", appdir, e->d_name);
            struct stat st;
            if (lstat(child, &st) == 0 && S_ISDIR(st.st_mode))
                rmrf_n(child);
        } else if (old_lock) {
            char child[PATH_MAX * 2];
            snprintf(child, sizeof(child), "%s/%s", appdir, e->d_name);
            unlink(child);
        }
    }
    closedir(d);
}

/* 缓存大小上限: exe 缓存总占用超限时，从最久未用的应用组开始驱逐
 * （永不删除当前应用组）。应用组列表动态分配，数量无硬编码上限。
 * 尽力而为，失败忽略。 */
static void enforce_cap_n(const char *exe_root, const char *cur_app)
{
    DIR *d = opendir(exe_root);
    if (!d)
        return;
    size_t capacity = 64;
    char (*names)[64] = malloc(capacity * sizeof(*names));
    uint64_t *sizes = malloc(capacity * sizeof(*sizes));
    uint64_t *times = malloc(capacity * sizeof(*times));
    if (!names || !sizes || !times) {
        free(names);
        free(sizes);
        free(times);
        closedir(d);
        return;
    }
    int n = 0;
    uint64_t total = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
            continue;
        char child[PATH_MAX * 2];
        snprintf(child, sizeof(child), "%s/%s", exe_root, e->d_name);
        struct stat st;
        if (lstat(child, &st) != 0 || !S_ISDIR(st.st_mode))
            continue;
        if ((size_t)n >= capacity) {
            size_t ncap = capacity * 2;
            char (*nn)[64] = realloc(names, ncap * sizeof(*nn));
            uint64_t *ns = realloc(sizes, ncap * sizeof(*ns));
            uint64_t *nt = realloc(times, ncap * sizeof(*nt));
            if (nn)
                names = nn;
            if (ns)
                sizes = ns;
            if (nt)
                times = nt;
            if (!nn || !ns || !nt)
                break; /* 内存不足: 用已收集的部分尽力清理 */
            capacity = ncap;
        }
        snprintf(names[n], sizeof(names[n]), "%s", e->d_name);
        sizes[n] = du_n(child);
        times[n] = lru_time_n(child);
        total += sizes[n];
        n++;
    }
    closedir(d);

    uint64_t cap = cache_cap_bytes();
    while (total > cap) {
        int victim = -1;
        for (int i = 0; i < n; i++) {
            if (sizes[i] == 0 || strcmp(names[i], cur_app) == 0)
                continue;
            if (victim < 0 || times[i] < times[victim])
                victim = i;
        }
        if (victim < 0)
            break;
        char child[PATH_MAX * 2];
        snprintf(child, sizeof(child), "%s/%s", exe_root, names[victim]);
        rmrf_n(child);
        total -= sizes[victim];
        sizes[victim] = 0;
    }
    free(names);
    free(sizes);
    free(times);
}

/* 进程级互斥（flock 锁文件），保证并发首解压串行 */
static int g_lockfd = -1;

static void lock_acquire_n(const char *app, const char *md5)
{
    const char *home = getenv("HOME");
    if (!home)
        home = "/tmp";
    char lockpath[PATH_MAX * 2];
    snprintf(lockpath, sizeof(lockpath), "%s/.cache/pythub/exe/%s/%s.lock",
             home, app, md5);
    mkdir_parents_n(lockpath);
    g_lockfd = open(lockpath, O_RDWR | O_CREAT, 0666);
    if (g_lockfd >= 0)
        flock(g_lockfd, LOCK_EX);
}

static void lock_release_n(void)
{
    if (g_lockfd >= 0) {
        flock(g_lockfd, LOCK_UN);
        close(g_lockfd);
        g_lockfd = -1;
    }
}

/* 原子解压: 解压到 <缓存>.part，校验启动器后原子改名就位 */
static void extract_atomic_n(const uint8_t *buf, uint64_t toc_offset, const char *cache)
{
    char part[PATH_MAX * 2];
    char entry[PATH_MAX];
    snprintf(part, sizeof(part), "%s.part", cache);
    if (access(part, F_OK) == 0)
        rmrf_n(part); /* 清理上次中断留下的临时目录 */
    extract_n(buf, toc_offset, part);
    snprintf(entry, sizeof(entry), "%s/%s", part, ENTRY_REL);
    if (access(entry, F_OK) != 0) {
        rmrf_n(part);
        die("解压失败：启动器未生成，归档可能已损坏");
    }
    if (access(cache, F_OK) == 0)
        rmrf_n(cache); /* 清理异常残留（完整缓存不存在但目录在） */
    if (rename(part, cache) != 0) {
        rmrf_n(part);
        die("解压目录就位失败");
    }
}

/* 缓存启动器完整性校验（符号链接感知），防缓存目录被篡改:
 * - 启动器条目为符号链接: 比较缓存 symlink 的目标串与归档条目内容；
 * - 普通条目: 比较缓存文件内容 MD5 与条目内容 MD5。 */
static int launcher_cache_ok_n(const char *cache, const uint8_t *buf, uint64_t toc_offset)
{
    uint64_t doff;
    uint32_t stored, raw;
    uint8_t flags;
    if (!toc_find(buf, toc_offset, ENTRY_REL_TOC, &doff, &stored, &raw, &flags))
        die("归档缺少启动器条目: %s", ENTRY_REL_TOC);
    uint8_t *infl = entry_payload(buf + doff, stored, raw, flags);
    const uint8_t *data = infl ? infl : buf + doff;
    size_t dlen = infl ? raw : stored;

    char full[PATH_MAX * 2];
    snprintf(full, sizeof(full), "%s/%s", cache, ENTRY_REL);
    int ok = 0;
    if (flags & FLAG_SYMLINK) {
        char tgt[1024];
        ssize_t n = readlink(full, tgt, sizeof(tgt) - 1);
        if (n > 0) {
            tgt[n] = 0;
            size_t want = dlen < sizeof(tgt) - 1 ? dlen : sizeof(tgt) - 1;
            ok = (strlen(tgt) == want) && memcmp(tgt, data, want) == 0;
        }
    } else {
        FILE *f = fopen(full, "rb");
        if (f) {
            fseek(f, 0, SEEK_END);
            long size = ftell(f);
            rewind(f);
            if (size > 0) {
                uint8_t *fdata = (uint8_t *)malloc((size_t)size);
                if (!fdata)
                    die("内存不足");
                if (fread(fdata, 1, (size_t)size, f) == (size_t)size) {
                    uint8_t got[16];
                    md5_1(fdata, (size_t)size, got);
                    uint8_t expect[16];
                    md5_1(data, dlen, expect);
                    ok = memcmp(got, expect, 16) == 0;
                }
                free(fdata);
            }
            fclose(f);
        }
    }
    free(infl);
    return ok;
}

static int spawn_n(const char *cache, int argc, char **argv)
{
    char entry[PATH_MAX];
    char pyz[PATH_MAX];
    snprintf(entry, sizeof(entry), "%s/%s", cache, ENTRY_REL);
    snprintf(pyz, sizeof(pyz), "%s/%s", cache, PYZ_REL);
    if (chdir(cache) != 0)
        die("无法进入缓存目录");

    /* Tcl/Tk 数据目录定位（PBS 构建的 libpython 静态内嵌 _tkinter，数据
     * 查找路径指向构建沙箱）: 存在则导出环境变量（不覆盖用户设置） */
    {
        char tclchk[PATH_MAX * 2];
        snprintf(tclchk, sizeof(tclchk), "%s/runtime/lib/tcl8.6/init.tcl", cache);
        if (access(tclchk, F_OK) == 0) {
            char tcllib[PATH_MAX * 2], tklib[PATH_MAX * 2];
            snprintf(tcllib, sizeof(tcllib), "%s/runtime/lib/tcl8.6", cache);
            snprintf(tklib, sizeof(tklib), "%s/runtime/lib/tk8.6", cache);
            setenv("TCL_LIBRARY", tcllib, 0);
            setenv("TK_LIBRARY", tklib, 0);
        }
    }

    char **child = (char **)malloc((size_t)(argc + 2) * sizeof(char *));
    child[0] = entry;
    child[1] = pyz;
    for (int i = 1; i < argc; i++)
        child[i + 1] = argv[i];
    child[argc + 1] = NULL;

    pid_t pid = fork();
    if (pid < 0)
        die("fork 失败");
    if (pid == 0) {
        execv(entry, child);
        _exit(127);
    }
    int st = 0;
    waitpid(pid, &st, 0);
    free(child);
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}

/* mem 模式启动（POSIX）: 引导器源码经 PYTHUB_BOOT 环境变量注入，
 * 应用字节码经 fd 3 管道直传解释器，全程内存执行。 */
static int spawn_mem_n(const char *cache, int argc, char **argv,
                       const uint8_t *buf, uint64_t toc_offset)
{
    uint8_t *boot, *payload;
    size_t bootlen, paylen;
    int boot_heap, pay_heap;
    mem_entry_load(buf, toc_offset, MEM_BOOT_REL, &boot, &bootlen, &boot_heap);
    mem_entry_load(buf, toc_offset, MEM_PAYLOAD_REL, &payload, &paylen, &pay_heap);

    char entry[PATH_MAX];
    snprintf(entry, sizeof(entry), "%s/%s", cache, ENTRY_REL);

    /* 引导器源码注入环境变量（execv 继承环境） */
    char *bootz = (char *)malloc(bootlen + 1);
    if (!bootz)
        die("内存不足");
    memcpy(bootz, boot, bootlen);
    bootz[bootlen] = 0;
    if (setenv("PYTHUB_BOOT", bootz, 1) != 0)
        die("无法设置引导器环境变量");
    free(bootz);
    if (boot_heap)
        free(boot);

    /* 子进程参数: <python> -I -c "<mini loader>" <self> <argv[1..]>
     * 子进程内 sys.argv = ['-c', <self>, args...]，引导器取 sys.argv[1:]
     * 作为应用 argv（argv[0] 为二进制自身路径，参数原样透传） */
    char selfpath[PATH_MAX];
    if (!realpath(argv[0], selfpath))
        die("无法定位自身路径");
    char **child = (char **)malloc((size_t)(argc + 5) * sizeof(char *));
    if (!child)
        die("内存不足");
    child[0] = entry;
    child[1] = (char *)"-I";
    child[2] = (char *)"-c";
    child[3] = (char *)MEM_MINI_LOADER;
    child[4] = selfpath;
    for (int i = 1; i < argc; i++)
        child[i + 4] = argv[i];
    child[argc + 4] = NULL;

    /* 子进程提前退出时避免父进程写管道触发 SIGPIPE 直接终止 */
    signal(SIGPIPE, SIG_IGN);

    int p[2];
    if (pipe(p) != 0)
        die("无法创建载荷管道");
    pid_t pid = fork();
    if (pid < 0)
        die("fork 失败");
    if (pid == 0) {
        close(p[1]);
        dup2(p[0], 3);
        if (p[0] != 3) /* pipe 读端可能恰好为 3: dup2 无操作，close 会误关载荷管道 */
            close(p[0]);
        execv(entry, child);
        _exit(127);
    }
    close(p[0]);
    size_t off = 0;
    while (off < paylen) {
        ssize_t w = write(p[1], payload + off, paylen - off);
        if (w <= 0)
            break; /* 子进程提前退出等情况: 尽力而为，退出码以子进程为准 */
        off += (size_t)w;
    }
    close(p[1]);
    int st = 0;
    waitpid(pid, &st, 0);
    free(child);
    if (pay_heap)
        free(payload);
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}

/* embed 内嵌解释器模式（需求 64, POSIX）: 引导器进程内 dlopen libpython +
 * dlsym 初始化解释器并执行引导器源码。应用结束经 SystemExit -> os._exit
 * 直接退出本进程（退出码即应用退出码）。 */
static int spawn_embed_n(const char *cache, int argc, char **argv,
                         const uint8_t *buf, uint64_t toc_offset)
{
    /* 解出 libpython 与 stdlib.zip 到缓存（仅两个文件，相比 mem 模式整个
     * 运行时目录树大幅减少落盘与首次解压开销） */
    uint8_t *data;
    size_t dlen;
    int heap;
    char pylib[PATH_MAX], stdlz[PATH_MAX];
    snprintf(pylib, sizeof(pylib), "%s/%s", cache, EMBED_PYLIB_CACHE);
    snprintf(stdlz, sizeof(stdlz), "%s/stdlib.zip", cache);
    /* 确保缓存目录存在（常规路径由 extract_atomic_n 创建，embed 跳过了） */
    {
        char parent[PATH_MAX];
        snprintf(parent, sizeof(parent), "%s", cache);
        char *slash = strrchr(parent, '/');
        if (slash) {
            *slash = 0;
            mkdir(parent, 0755); /* <exe_root>/<app> */
        }
        mkdir(cache, 0755); /* <exe_root>/<app>/<md5> */
    }
    mem_entry_load(buf, toc_offset, EMBED_PYLIB_REL, &data, &dlen, &heap);
    write_file_n(pylib, data, dlen);
    if (heap)
        free(data);
    mem_entry_load(buf, toc_offset, EMBED_STDLZ_REL, &data, &dlen, &heap);
    write_file_n(stdlz, data, dlen);
    if (heap)
        free(data);

    /* Tcl/Tk 数据 zip（可选常规条目）: 落盘到缓存，runner 解出 tcl8.6/tk8.6
     * 真实文件目录并设置 TCL_LIBRARY/TK_LIBRARY（Tcl 无法走 zipimport） */
    {
        uint64_t toff;
        uint32_t tst, trw;
        uint8_t tfl;
        if (toc_find(buf, toc_offset, EMBED_TCLTK_REL, &toff, &tst, &trw, &tfl)) {
            uint8_t *tk = entry_payload(buf + toff, tst, trw, tfl);
            const uint8_t *tksrc = tk ? tk : (buf + toff);
            size_t tklen = tk ? trw : tst;
            char tkp[PATH_MAX];
            snprintf(tkp, sizeof(tkp), "%s/%s", cache, EMBED_TCLTK_REL);
            write_file_n(tkp, tksrc, tklen);
            if (tk)
                free(tk);
        }
    }

    char selfpath[PATH_MAX];
    if (!realpath(argv[0], selfpath))
        die("无法定位自身路径");

    /* 环境变量注入（runner 内 pop）: PYTHONPATH=stdlib.zip（zipimport 直接
     * 从 zip 导入，含 lib-dynload 扩展模块）；PYTHUB_SELF 供 runner 打开
     * 自身归档解析载荷；argv 逐项 base64 逗号连接（env 无法携带 \0） */
    setenv("PYTHONPATH", stdlz, 1);
    setenv("PYTHONIOENCODING", "utf-8", 1); /* 嵌入式环境 locale 检测可能失败 */
    setenv("PYTHUB_SELF", selfpath, 1);
    setenv("PYTHUB_EXE_CACHE", cache, 1); /* 缓存目录（runner 释放 tcl/tk 用） */
    size_t cap = (size_t)argc * 4 * PATH_MAX + 16;
    char *b64 = (char *)malloc(cap);
    if (!b64)
        die("内存不足");
    size_t bo = 0;
    for (int i = 0; i < argc; i++) {
        if (i > 0)
            b64[bo++] = ',';
        bo += b64_encode((const uint8_t *)argv[i], strlen(argv[i]), b64 + bo);
    }
    b64[bo] = 0;
    setenv("PYTHUB_EMBED_ARGV", b64, 1);
    free(b64);

    /* 进程内加载解释器（dlopen 缓存中的 libpython 副本） */
    void *h = dlopen(pylib, RTLD_NOW | RTLD_GLOBAL);
    if (!h)
        die("无法加载 libpython: %s", dlerror());
    void (*py_init)(void) = (void (*)(void))dlsym(h, "Py_Initialize");
    int (*py_run)(const char *) =
        (int (*)(const char *))dlsym(h, "PyRun_SimpleString");
    if (!py_init || !py_run)
        die("libpython 缺少嵌入式初始化符号");

    uint8_t *boot;
    size_t bootlen;
    int boot_heap;
    mem_entry_load(buf, toc_offset, MEM_BOOT_REL, &boot, &bootlen, &boot_heap);
    char *bootz = (char *)malloc(bootlen + 1);
    if (!bootz)
        die("内存不足");
    memcpy(bootz, boot, bootlen);
    bootz[bootlen] = 0;

    py_init();
    int r = py_run(bootz); /* runner 以 os._exit 收尾，正常不会到达这里 */
    if (boot_heap)
        free(boot);
    free(bootz);
    return r == 0 ? 0 : 1;
}

#ifdef PYTHUB_STATIC
/* static 内存模式（需求 64 第四档）: 解释器静态链接进本二进制（链接期
 * 符号直接可用，无需 dlopen），stdlib 以 frozen 字节码注册到
 * PyImport_FrozenModules——除 GUI 应用 tcl/tk 数据外零落盘。
 * PYTHUB_STATIC 分支编译时需 -I <PBS runtime>/include/python3.x，
 * struct _frozen 等定义直接取自目标运行时头文件（跨版本安全）。 */
#include <Python.h>

static int spawn_static_n(const char *cache, int argc, char **argv,
                          const uint8_t *buf, uint64_t toc_offset)
{
    char parent[PATH_MAX], selfpath[PATH_MAX];
    snprintf(parent, sizeof(parent), "%s", cache);
    {
        char *slash = strrchr(parent, '/');
        if (slash) {
            *slash = 0;
            mkdir(parent, 0755);
        }
        mkdir(cache, 0755); /* tcl/tk 数据释放目录（应用引用 tkinter 时） */
    }
    if (!realpath(argv[0], selfpath))
        die("无法定位自身路径");

    /* Tcl/Tk 数据 zip（可选常规条目）: 落盘到缓存（Tcl 需要真实文件，
     * 是 static 模式唯一的落盘项） */
    {
        uint64_t toff;
        uint32_t tst, trw;
        uint8_t tfl;
        if (toc_find(buf, toc_offset, EMBED_TCLTK_REL, &toff, &tst, &trw, &tfl)) {
            uint8_t *tk = entry_payload(buf + toff, tst, trw, tfl);
            const uint8_t *tksrc = tk ? tk : (buf + toff);
            size_t tklen = tk ? trw : tst;
            char tkp[PATH_MAX];
            snprintf(tkp, sizeof(tkp), "%s/%s", cache, EMBED_TCLTK_REL);
            write_file_n(tkp, tksrc, tklen);
            if (tk)
                free(tk);
        }
    }

    /* stdlib frozen 字节码 → PyImport_FrozenModules（在 Py_Initialize 之前
     * 注册: encodings 等启动模块即从内存加载，全程不访问文件系统 stdlib）。
     * blob 格式（mempack.STDLZF_BLOB_FMT）:
     *   [u32 count] { [u16 namelen][name][u8 is_pkg][u32 codelen][marshal] } */
    {
        uint8_t *frozen;
        size_t flen;
        int fheap;
        mem_entry_load(buf, toc_offset, EMBED_STDLZF_REL, &frozen, &flen, &fheap);
        unsigned int count = 0;
        memcpy(&count, frozen, 4);
        struct _frozen *table = (struct _frozen *)calloc(count + 1, sizeof(struct _frozen));
        if (!table)
            die("内存不足");
        const unsigned char *p = frozen + 4;
        for (unsigned int i = 0; i < count; i++) {
            unsigned short nl;
            memcpy(&nl, p, 2); p += 2;
            char *name = (char *)malloc((size_t)nl + 1);
            if (!name)
                die("内存不足");
            memcpy(name, p, (size_t)nl);
            name[nl] = 0;
            p += nl;
            unsigned int cl;
            unsigned char ispkg = *p; p += 1;
            memcpy(&cl, p, 4); p += 4;
            table[i].name = name;
            table[i].code = p; /* marshal 字节码直接指向内存 blob（零拷贝） */
            table[i].size = (int)cl;
            table[i].is_package = ispkg;
#if PY_VERSION_HEX >= 0x030C0000
            table[i].get_code = NULL; /* 直接使用 code/size 内存数据 */
#endif
            p += cl;
        }
        /* 合并默认表: 3.11 及以下 PyImport_FrozenModules 含 importlib
         * bootstrap（pre-init 非 NULL）；3.12+ 拆分内部表（pre-init 为
         * NULL，无需合并）。frozen 数据必须常驻内存（fheap 不释放，
         * code 指针指向其中，引导器进程生命周期即应用生命周期）。 */
        if (PyImport_FrozenModules) {
            int n_orig = 0;
            while (PyImport_FrozenModules[n_orig].name)
                n_orig++;
            struct _frozen *merged = (struct _frozen *)calloc(
                (size_t)n_orig + count + 1, sizeof(struct _frozen));
            if (!merged)
                die("内存不足");
            memcpy(merged, PyImport_FrozenModules, (size_t)n_orig * sizeof(struct _frozen));
            memcpy(merged + n_orig, table, (size_t)count * sizeof(struct _frozen));
            table = merged;
        }
        PyImport_FrozenModules = table;
        (void)flen;
    }

    /* 环境变量注入（runner 内 pop）: PYTHUB_SELF 供 runner 解析自身归档
     * 载荷；PYTHUB_EXE_CACHE 供 tcl/tk 释放；argv 逐项 base64 逗号连接 */
    setenv("PYTHONIOENCODING", "utf-8", 1); /* 嵌入式环境 locale 检测可能失败 */
    setenv("PYTHUB_SELF", selfpath, 1);
    setenv("PYTHUB_EXE_CACHE", cache, 1);
    size_t cap = (size_t)argc * 4 * PATH_MAX + 16;
    char *b64 = (char *)malloc(cap);
    if (!b64)
        die("内存不足");
    size_t bo = 0;
    for (int i = 0; i < argc; i++) {
        if (i > 0)
            b64[bo++] = ',';
        bo += b64_encode((const uint8_t *)argv[i], strlen(argv[i]), b64 + bo);
    }
    b64[bo] = 0;
    setenv("PYTHUB_EMBED_ARGV", b64, 1);
    free(b64);

    uint8_t *boot;
    size_t bootlen;
    int boot_heap;
    mem_entry_load(buf, toc_offset, MEM_BOOT_REL, &boot, &bootlen, &boot_heap);
    char *bootz = (char *)malloc(bootlen + 1);
    if (!bootz)
        die("内存不足");
    memcpy(bootz, boot, bootlen);
    bootz[bootlen] = 0;

    Py_Initialize(); /* 解释器就在本二进制内——链接期符号，直接调用 */
    int r = PyRun_SimpleString(bootz); /* runner 以 os._exit 收尾 */
    if (boot_heap)
        free(boot);
    free(bootz);
    return r == 0 ? 0 : 1;
}
#endif /* PYTHUB_STATIC */

int main(int argc, char **argv)
{
    size_t len;
    uint8_t *buf = read_self(argc > 0 ? argv[0] : ".", &len);
    if (len < COOKIE_SIZE)
        die("文件过小，归档不完整");
    const uint8_t *tail = buf + len - COOKIE_SIZE;
    if (memcmp(tail, MAGIC, 8) != 0)
        die("cookie 校验失败，不是 Pythub 自研单文件归档");
    uint64_t payload_offset = rd64(tail + 8);
    uint64_t toc_offset = rd64(tail + 16);
    uint64_t toc_size = rd64(tail + 24);
    char md5[33];
    memcpy(md5, tail + 32, 32);
    md5[32] = 0;
    char app[APP_FIELD_SIZE + 1];
    memcpy(app, tail + 64, APP_FIELD_SIZE);
    app[APP_FIELD_SIZE] = 0;
    sanitize_app(app);

    verify_body(buf, md5, payload_offset, toc_offset, toc_size);

    const char *home = getenv("HOME");
    if (!home)
        home = "/tmp";
    char exe_root[PATH_MAX * 2];
    char appdir[PATH_MAX * 2];
    char cache[PATH_MAX * 2];
    snprintf(exe_root, sizeof(exe_root), "%s/.cache/pythub/exe", home);
    snprintf(appdir, sizeof(appdir), "%s/%s", exe_root, app);
    snprintf(cache, sizeof(cache), "%s/%s", appdir, md5);

    /* static 模式检测（需求 64 第四档）: stdlzf 条目存在即 static——解释器
     * 已静态链接进本二进制（PYTHUB_STATIC 编译），零落盘执行 */
    {
        uint64_t mdoff;
        uint32_t mst, mrw;
        uint8_t mfl;
        if (toc_find(buf, toc_offset, EMBED_STDLZF_REL, &mdoff, &mst, &mrw, &mfl)) {
#ifdef PYTHUB_STATIC
            int rc = spawn_static_n(cache, argc, argv, buf, toc_offset);
            free(buf);
            return rc;
#else
            die("本引导器未启用 static 模式（PYTHUB_STATIC 编译宏缺失）");
#endif
        }
    }

    /* embed 模式检测（需求 64）: 必须在常规解压之前——embed 归档没有
     * runtime/ENTRY 常规条目（载荷为 libpython+stdlib 两个 FLAG_MEM 条目，
     * 由 spawn_embed 自行落盘），走到 extract 会误报"启动器未生成"。
     * mem 模式（需求 23）归档含完整 runtime 常规条目，走常规解压后再分发。 */
    {
        uint64_t mdoff;
        uint32_t mst, mrw;
        uint8_t mfl;
        if (toc_find(buf, toc_offset, EMBED_PYLIB_REL, &mdoff, &mst, &mrw, &mfl)) {
            int rc = spawn_embed_n(cache, argc, argv, buf, toc_offset);
            free(buf);
            return rc;
        }
    }
    int mem_mode = 0;
    {
        uint64_t mdoff;
        uint32_t mst, mrw;
        uint8_t mfl;
        mem_mode = toc_find(buf, toc_offset, MEM_BOOT_REL, &mdoff, &mst, &mrw, &mfl);
    }

    char entry[PATH_MAX];
    snprintf(entry, sizeof(entry), "%s/%s", cache, ENTRY_REL);
    int re_extract = 0;
    if (access(entry, F_OK) != 0) {
        boot_log("首次运行，正在解压...");
        re_extract = 1;
    } else if (!launcher_cache_ok_n(cache, buf, toc_offset)) {
        boot_log("缓存完整性校验失败，正在重新解压...");
        re_extract = 1;
    }
    if (re_extract) {
        lock_acquire_n(app, md5);
        /* 双重检查：并发实例可能已完成/修复解压 */
        if (access(entry, F_OK) != 0 || !launcher_cache_ok_n(cache, buf, toc_offset))
            extract_atomic_n(buf, toc_offset, cache);
        lock_release_n();
        if (access(entry, F_OK) != 0)
            die("解压失败：启动器未生成，归档可能已损坏");
    }
    /* P2 缓存清理: 每次运行刷新 LRU 时间戳；新版本首解压后清理同应用旧
     * md5 目录，并按缓存总大小上限驱逐最久未用的应用组（尽力而为） */
    touch_stamp_n(cache);
    if (re_extract) {
        cleanup_app_n(appdir, md5);
        enforce_cap_n(exe_root, app);
    }
    /* 常规解压执行（embed 已在上方提前分发）: mem 模式需先解出 runtime
     * 再启动（spawn_mem_n 的子进程 exec cache/runtime/bin/python3） */
    {
        int rc;
        if (mem_mode)
            rc = spawn_mem_n(cache, argc, argv, buf, toc_offset);
        else
            rc = spawn_n(cache, argc, argv);
        free(buf);
        return rc;
    }
}
#endif
