/*
 * slimpack launcher stub.
 *
 * Two build modes, selected at compile time:
 *   - onedir  (default): the python runtime (python3XX.dll, *.pyd, the module
 *     zip and __main__.pyc) sits in the same folder as this .exe.
 *   - onefile (-DSLIMPACK_ONEFILE): an LZMA-compressed archive of that whole
 *     runtime is appended to this .exe as an overlay. At launch we decompress
 *     it once into %TEMP%\slimpack_<hash>\ and run from there.
 *
 * In both cases we load python3XX.dll dynamically and hand control to
 * Py_Main(). Path configuration is driven by a pythonXX._pth file placed next
 * to the DLL (the mechanism CPython's embeddable distribution uses), so no
 * fragile PyConfig struct layout is baked into this binary.
 *
 * Build-time constants come from the generated header stub_config.h.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "stub_config.h"

#ifdef SLIMPACK_ONEFILE
#include "LzmaDec.h"
#endif

typedef int (*Py_Main_t)(int, wchar_t **);

static void fail(const wchar_t *msg)
{
    DWORD err = GetLastError();
    wchar_t buf[1024];
    _snwprintf(buf, 1024, L"slimpack: %ls (error %lu)", msg, (unsigned long)err);
    /* Show on console if attached, and a message box as a fallback. */
    fwprintf(stderr, L"%ls\n", buf);
    MessageBoxW(NULL, buf, L"slimpack", MB_ICONERROR | MB_OK);
    ExitProcess(1);
}

/* dir name of a full path, in place (truncate at last backslash). */
static void dirname_inplace(wchar_t *path)
{
    wchar_t *p = wcsrchr(path, L'\\');
    if (p)
        *p = 0;
}

#ifdef SLIMPACK_ONEFILE

/* ---- LZMA self-extractor (onefile) ------------------------------------- */

static void *SzAlloc(ISzAllocPtr p, size_t size) { (void)p; return malloc(size); }
static void SzFree(ISzAllocPtr p, void *addr) { (void)p; free(addr); }
static ISzAlloc g_Alloc = { SzAlloc, SzFree };

/* Trailer appended to the exe, little-endian, in this order:
 *   props[5], u64 raw_size, u64 comp_size, magic[8] = "SLIMPK01"
 * The compressed blob is the comp_size bytes immediately before props. */
#define TRAILER_SIZE (5 + 8 + 8 + 8)
static const char MAGIC[8] = { 'S','L','I','M','P','K','0','1' };

static unsigned long long rd_u64(const unsigned char *p)
{
    unsigned long long v = 0;
    for (int i = 7; i >= 0; i--)
        v = (v << 8) | p[i];
    return v;
}
static unsigned rd_u32(const unsigned char *p)
{
    return (unsigned)p[0] | ((unsigned)p[1] << 8) | ((unsigned)p[2] << 16) | ((unsigned)p[3] << 24);
}
static unsigned rd_u16(const unsigned char *p)
{
    return (unsigned)p[0] | ((unsigned)p[1] << 8);
}

static void mkdirs(const wchar_t *dir)
{
    /* create every component of dir */
    wchar_t tmp[MAX_PATH * 2];
    wcsncpy(tmp, dir, MAX_PATH * 2 - 1);
    tmp[MAX_PATH * 2 - 1] = 0;
    for (wchar_t *p = tmp + 3; *p; p++) {  /* skip drive "C:\" */
        if (*p == L'\\') {
            *p = 0;
            CreateDirectoryW(tmp, NULL);
            *p = L'\\';
        }
    }
    CreateDirectoryW(tmp, NULL);
}

static void write_file(const wchar_t *root, const char *name_utf8, unsigned name_len,
                       const unsigned char *data, unsigned data_len)
{
    /* Convert utf-8 name (with '/' separators) to a wide path under root. */
    wchar_t wname[MAX_PATH * 2];
    int wn = MultiByteToWideChar(CP_UTF8, 0, name_utf8, (int)name_len, wname, MAX_PATH * 2 - 1);
    wname[wn] = 0;
    for (int i = 0; i < wn; i++)
        if (wname[i] == L'/')
            wname[i] = L'\\';

    wchar_t full[MAX_PATH * 2];
    _snwprintf(full, MAX_PATH * 2, L"%ls\\%ls", root, wname);

    /* ensure parent dir exists */
    wchar_t parent[MAX_PATH * 2];
    wcsncpy(parent, full, MAX_PATH * 2 - 1);
    parent[MAX_PATH * 2 - 1] = 0;
    dirname_inplace(parent);
    mkdirs(parent);

    HANDLE h = CreateFileW(full, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        fail(L"could not write extracted file");
    DWORD written = 0;
    if (data_len && !WriteFile(h, data, data_len, &written, NULL))
        fail(L"short write during extraction");
    CloseHandle(h);
}

/* Read the whole of our own exe into memory. Returns size via *out_size. */
static unsigned char *read_self(const wchar_t *exepath, unsigned long long *out_size)
{
    HANDLE h = CreateFileW(exepath, GENERIC_READ, FILE_SHARE_READ, NULL,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        fail(L"cannot open own executable");
    LARGE_INTEGER sz;
    GetFileSizeEx(h, &sz);
    unsigned char *buf = (unsigned char *)malloc((size_t)sz.QuadPart);
    if (!buf)
        fail(L"out of memory reading executable");
    unsigned long long done = 0;
    while (done < (unsigned long long)sz.QuadPart) {
        DWORD chunk = (DWORD)((sz.QuadPart - done > 0x400000) ? 0x400000 : (sz.QuadPart - done));
        DWORD got = 0;
        if (!ReadFile(h, buf + done, chunk, &got, NULL) || got == 0)
            fail(L"read error on own executable");
        done += got;
    }
    CloseHandle(h);
    *out_size = (unsigned long long)sz.QuadPart;
    return buf;
}

static void extract_overlay(const wchar_t *exepath, const wchar_t *destdir)
{
    unsigned long long total = 0;
    unsigned char *self = read_self(exepath, &total);
    if (total < TRAILER_SIZE)
        fail(L"executable too small to contain payload");

    const unsigned char *tr = self + (total - TRAILER_SIZE);
    const unsigned char *props = tr;
    unsigned long long raw_size = rd_u64(tr + 5);
    unsigned long long comp_size = rd_u64(tr + 5 + 8);
    if (memcmp(tr + 5 + 16, MAGIC, 8) != 0)
        fail(L"payload magic not found (corrupt executable?)");

    const unsigned char *blob = self + (total - TRAILER_SIZE - comp_size);

    unsigned char *raw = (unsigned char *)malloc((size_t)raw_size);
    if (!raw)
        fail(L"out of memory for payload");

    SizeT destLen = (SizeT)raw_size;
    SizeT srcLen = (SizeT)comp_size;
    ELzmaStatus status;
    SRes res = LzmaDecode(raw, &destLen, blob, &srcLen,
                          props, 5, LZMA_FINISH_END, &status, &g_Alloc);
    if (res != SZ_OK || destLen != raw_size)
        fail(L"payload decompression failed");
    free(self);

    /* parse archive: u32 count, then per file: u16 nlen, name, u32 dlen, data */
    const unsigned char *p = raw;
    unsigned count = rd_u32(p); p += 4;
    for (unsigned i = 0; i < count; i++) {
        unsigned nlen = rd_u16(p); p += 2;
        const char *name = (const char *)p; p += nlen;
        unsigned dlen = rd_u32(p); p += 4;
        const unsigned char *data = p; p += dlen;
        write_file(destdir, name, nlen, data, dlen);
    }
    free(raw);
}

#endif /* SLIMPACK_ONEFILE */

int wmain(void)
{
    wchar_t exepath[MAX_PATH * 2];
    GetModuleFileNameW(NULL, exepath, MAX_PATH * 2);

    wchar_t runtime_dir[MAX_PATH * 2];

#ifdef SLIMPACK_ONEFILE
    wchar_t tempbase[MAX_PATH * 2];
    GetTempPathW(MAX_PATH * 2, tempbase);
    _snwprintf(runtime_dir, MAX_PATH * 2, L"%lsslimpack_%hs", tempbase, SLIMPACK_HASH);

    wchar_t sentinel[MAX_PATH * 2];
    _snwprintf(sentinel, MAX_PATH * 2, L"%ls\\.slimpack_ok", runtime_dir);
    if (GetFileAttributesW(sentinel) == INVALID_FILE_ATTRIBUTES) {
        CreateDirectoryW(runtime_dir, NULL);
        extract_overlay(exepath, runtime_dir);
        HANDLE h = CreateFileW(sentinel, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                               FILE_ATTRIBUTE_NORMAL, NULL);
        if (h != INVALID_HANDLE_VALUE)
            CloseHandle(h);
    }
#else
    wcsncpy(runtime_dir, exepath, MAX_PATH * 2 - 1);
    runtime_dir[MAX_PATH * 2 - 1] = 0;
    dirname_inplace(runtime_dir);
#endif

    /* Make the runtime dir the preferred search path for dependent DLLs
     * (vcruntime, and any .pyd dependencies loaded later by Python). */
    SetDllDirectoryW(runtime_dir);

    wchar_t dllpath[MAX_PATH * 2];
    _snwprintf(dllpath, MAX_PATH * 2, L"%ls\\%ls", runtime_dir, SLIMPACK_PYDLL);
    HMODULE py = LoadLibraryExW(dllpath, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!py)
        py = LoadLibraryW(dllpath);
    if (!py)
        fail(L"could not load the Python runtime DLL");

    Py_Main_t pPy_Main = (Py_Main_t)(void *)GetProcAddress(py, "Py_Main");
    if (!pPy_Main)
        fail(L"Py_Main not found in runtime DLL");

    wchar_t scriptpath[MAX_PATH * 2];
    _snwprintf(scriptpath, MAX_PATH * 2, L"%ls\\%ls", runtime_dir, SLIMPACK_SCRIPT);

    /* Build argv for Py_Main: [exe, script, <forwarded user args...>] */
    int real_argc = 0;
    wchar_t **real_argv = CommandLineToArgvW(GetCommandLineW(), &real_argc);
    int extra = (real_argc > 1) ? (real_argc - 1) : 0;
    int new_argc = 2 + extra;
    wchar_t **argv = (wchar_t **)malloc(sizeof(wchar_t *) * (new_argc + 1));
    argv[0] = exepath;
    argv[1] = scriptpath;
    for (int i = 0; i < extra; i++)
        argv[2 + i] = real_argv[1 + i];
    argv[new_argc] = NULL;

    int rc = pPy_Main(new_argc, argv);
    return rc;
}
