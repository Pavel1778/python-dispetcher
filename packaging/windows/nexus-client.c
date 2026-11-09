/*
 * NEXUS Windows launcher.
 *
 * A small native Win32 program that opens the configured NEXUS backend URL in
 * the default browser. It is a launcher, not a second client: authentication
 * happens in the browser against the same server, so this program stores no
 * credential.
 *
 * The URL is read from the NEXUS_URL environment variable, else
 * %APPDATA%\nexus\client.ini ([nexus] url=...), else a localhost default.
 * Only http/https are accepted, so the launcher cannot be pointed at a
 * file:// or other scheme.
 *
 * Build: x86_64-w64-mingw32-gcc -O2 -mwindows -o nexus-client.exe nexus-client.c
 */
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <shlobj.h>
#include <stdio.h>
#include <string.h>

#define DEFAULT_URL L"http://127.0.0.1:8000"
#define MAX_URL 2048

static int is_http(const wchar_t *url) {
    return (wcsncmp(url, L"http://", 7) == 0) || (wcsncmp(url, L"https://", 8) == 0);
}

static void trim(wchar_t *s) {
    size_t n = wcslen(s);
    while (n > 0 && (s[n - 1] == L'\n' || s[n - 1] == L'\r' || s[n - 1] == L' ' ||
                     s[n - 1] == L'\t')) {
        s[--n] = L'\0';
    }
}

/* Read [nexus] url=... from %APPDATA%\nexus\client.ini. Returns 0 on success. */
static int read_config(wchar_t *out, size_t cap) {
    wchar_t appdata[MAX_PATH];
    wchar_t path[MAX_PATH * 2];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_APPDATA, NULL, 0, appdata))) {
        return 1;
    }
    _snwprintf(path, MAX_PATH * 2, L"%ls\\nexus\\client.ini", appdata);
    FILE *f = _wfopen(path, L"r, ccs=UTF-8");
    if (!f) {
        return 1;
    }
    wchar_t line[MAX_URL];
    int found = 1;
    while (fgetws(line, MAX_URL, f)) {
        wchar_t *p = line;
        while (*p == L' ' || *p == L'\t') p++;
        if (_wcsnicmp(p, L"url=", 4) == 0) {
            wcsncpy(out, p + 4, cap - 1);
            out[cap - 1] = L'\0';
            trim(out);
            found = 0;
            break;
        }
    }
    fclose(f);
    return found;
}

static void resolve_url(wchar_t *out, size_t cap) {
    DWORD n = GetEnvironmentVariableW(L"NEXUS_URL", out, (DWORD)cap);
    if (n > 0 && n < cap && is_http(out)) {
        return;
    }
    if (read_config(out, cap) == 0 && is_http(out)) {
        return;
    }
    wcsncpy(out, DEFAULT_URL, cap - 1);
    out[cap - 1] = L'\0';
}

int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrev, PWSTR pCmdLine, int nCmdShow) {
    (void)hInstance; (void)hPrev; (void)nCmdShow;
    wchar_t url[MAX_URL];
    /* A URL may also be passed as the first command-line argument. */
    if (pCmdLine != NULL && *pCmdLine != L'\0') {
        wcsncpy(url, pCmdLine, MAX_URL - 1);
        url[MAX_URL - 1] = L'\0';
        trim(url);
    } else {
        resolve_url(url, MAX_URL);
    }
    if (!is_http(url)) {
        MessageBoxW(NULL, L"Refusing to open a non-http(s) URL.", L"NEXUS",
                    MB_OK | MB_ICONERROR);
        return 2;
    }
    HINSTANCE rc = ShellExecuteW(NULL, L"open", url, NULL, NULL, SW_SHOWNORMAL);
    if ((INT_PTR)rc <= 32) {
        wchar_t msg[MAX_URL + 128];
        _snwprintf(msg, MAX_URL + 128,
                   L"Could not launch a browser.\n\nOpen this URL manually:\n%ls", url);
        MessageBoxW(NULL, msg, L"NEXUS", MB_OK | MB_ICONWARNING);
        return 1;
    }
    return 0;
}
