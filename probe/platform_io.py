"""Private local files and a process lock on POSIX and native Windows.

Windows permissions use a protected DACL for the current user and SYSTEM.
Reparse points, alternate data streams and redirected final handles fail closed.
The threat boundary excludes another process running as the same user.
"""
import contextlib
import ctypes
import errno
import os
from pathlib import Path
import re
import stat
import sys

WINDOWS = sys.platform == "win32"


def safe_path(path):
    path = Path(os.path.abspath(path))
    if WINDOWS and (str(path).startswith("\\\\") or any(
            re.search(r'[:*?"<>|\x00-\x1f]', p) or p.endswith((".", " ")) or
            p.split('.')[0].upper() in {'CON','PRN','AUX','NUL','CLOCK$',
                *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))}
            for p in path.parts[1:])):
        raise OSError("Local regular paths without streams are required")
    for item in reversed((path, *path.parents) if WINDOWS else (path,)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("Reparse points and symbolic links are forbidden")
    return path


class _WinFiles:
    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        def bind(dll, name, restype, *args):
            fn = getattr(dll, name); fn.restype = restype; fn.argtypes = args
            return fn
        self.current = bind(self.k, "GetCurrentProcess", w.HANDLE)
        self.close = bind(self.k, "CloseHandle", w.BOOL, w.HANDLE)
        self.free = bind(self.k, "LocalFree", w.HLOCAL, w.HLOCAL)
        self.open_token = bind(self.a, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
        self.token_info = bind(self.a, "GetTokenInformation", w.BOOL, w.HANDLE, ctypes.c_int,
                               ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD))
        self.sid_string = bind(self.a, "ConvertSidToStringSidW", w.BOOL, ctypes.c_void_p, ctypes.POINTER(w.LPWSTR))
        self.from_sddl = bind(self.a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL,
                              w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD))
        self.to_sddl = bind(self.a, "ConvertSecurityDescriptorToStringSecurityDescriptorW", w.BOOL,
                            ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.DWORD))
        self.get_dacl = bind(self.a, "GetSecurityDescriptorDacl", w.BOOL, ctypes.c_void_p,
                             ctypes.POINTER(w.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL))
        self.set_security = bind(self.a, "SetSecurityInfo", w.DWORD, w.HANDLE, ctypes.c_int, w.DWORD,
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        self.get_security = bind(self.a, "GetSecurityInfo", w.DWORD, w.HANDLE, ctypes.c_int, w.DWORD,
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        self.create_file = bind(self.k, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD,
                                ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE)
        self.create_dir = bind(self.k, "CreateDirectoryW", w.BOOL, w.LPCWSTR, ctypes.c_void_p)
        self.attributes = bind(self.k, "GetFileInformationByHandleEx", w.BOOL, w.HANDLE,
                               ctypes.c_int, ctypes.c_void_p, w.DWORD)
        self.final_path = bind(self.k, "GetFinalPathNameByHandleW", w.DWORD, w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD)
        class SA(ctypes.Structure):
            _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", w.BOOL)]
        self.SA = SA
        token = w.HANDLE()
        self.check(self.open_token(self.current(), 8, ctypes.byref(token)))
        try:
            size = w.DWORD()
            self.token_info(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            self.check(self.token_info(token, 1, buffer, size, ctypes.byref(size)))
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            value = w.LPWSTR()
            self.check(self.sid_string(sid, ctypes.byref(value)))
            try: self.sid = value.value
            finally: self.free(value)
        finally: self.close(token)
        # Windows can render the same token SID as an SDDL alias (for example
        # LA for the built-in Administrator account). Derive the trusted spelling
        # from our own descriptor; never broaden access to a fixed alias list.
        with self.descriptor() as sd:
            value = w.LPWSTR()
            self.check(self.to_sddl(sd, 1, 4, ctypes.byref(value), None))
            try:
                self.allowed_sddl_sids = frozenset(
                    ace.split(';')[-1] for ace in re.findall(r'\(([^)]*)\)', value.value))
            finally: self.free(value)

    @staticmethod
    def check(value):
        if not value: raise ctypes.WinError(ctypes.get_last_error())

    def sddl(self, directory=False):
        flags = "OICI" if directory else ""
        return f"D:P(A;{flags};FA;;;{self.sid})(A;{flags};FA;;;SY)"

    @contextlib.contextmanager
    def descriptor(self, directory=False):
        pointer = ctypes.c_void_p()
        self.check(self.from_sddl(self.sddl(directory), 1, ctypes.byref(pointer), None))
        try: yield pointer
        finally: self.free(pointer)

    def validate_handle(self, handle, path, directory=False):
        attrs = (self.w.DWORD * 2)()
        self.check(self.attributes(handle, 9, attrs, ctypes.sizeof(attrs)))
        if attrs[0] & 0x400 or bool(attrs[0] & 0x10) != directory:
            raise OSError("Regular unredirected file/directory required")
        buffer = ctypes.create_unicode_buffer(32768)
        count = self.final_path(handle, buffer, len(buffer), 0)
        if not count or count >= len(buffer): raise OSError("Cannot validate final file path")
        actual = buffer.value.removeprefix("\\\\?\\")
        if os.path.normcase(os.path.normpath(actual)) != os.path.normcase(str(path)):
            raise OSError("Final file path was redirected")

    def protect_handle(self, handle, directory=False):
        with self.descriptor(directory) as sd:
            present, defaulted, acl = self.w.BOOL(), self.w.BOOL(), ctypes.c_void_p()
            self.check(self.get_dacl(sd, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)))
            code = self.set_security(handle, 1, 4 | 0x80000000, None, None, acl, None)
            if code: raise ctypes.WinError(code)

    @contextlib.contextmanager
    def handle(self, path, access, disposition=3, directory=False, sd=None):
        sa = self.SA(ctypes.sizeof(self.SA), sd, False) if sd else None
        handle = self.create_file(str(path), access, 7, ctypes.byref(sa) if sa else None,
                                  disposition, 0x00200000 | (0x02000000 if directory else 0x80), None)
        if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.validate_handle(handle, path, directory)
            yield handle
        finally: self.close(handle)

    def private(self, path, directory=False):
        with self.handle(path, 0x00020000, directory=directory) as handle:
            acl, sd = ctypes.c_void_p(), ctypes.c_void_p()
            code = self.get_security(handle, 1, 4, None, None, ctypes.byref(acl), None, ctypes.byref(sd))
            if code: raise ctypes.WinError(code)
            value = self.w.LPWSTR()
            try:
                self.check(self.to_sddl(sd, 1, 4, ctypes.byref(value), None))
                text = value.value
                aces = re.findall(r"\(([^)]*)\)", text)
                return text.startswith("D:P") and len(aces) == 2 and all(
                    a.split(";")[0] == "A" and a.split(";")[-1] in self.allowed_sddl_sids for a in aces)
            finally:
                if value: self.free(value)
                self.free(sd)


_win = None


def win_files():
    global _win
    if _win is None: _win = _WinFiles()
    return _win


def private_dir(path):
    path = safe_path(path)
    if WINDOWS:
        win = win_files()
        if not path.exists():
            if not path.parent.exists(): private_dir(path.parent)
            with win.descriptor(True) as sd:
                sa = win.SA(ctypes.sizeof(win.SA), sd, False)
                if not win.create_dir(str(path), ctypes.byref(sa)) and ctypes.get_last_error() != 183:
                    raise ctypes.WinError(ctypes.get_last_error())
        with win.handle(path, 0x00060000, directory=True) as handle: win.protect_handle(handle, True)
    else:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return path


def open_private(path, flags, mode=0o600):
    path = safe_path(path)
    if not WINDOWS:
        return os.open(path, flags | os.O_NOFOLLOW, mode)
    import msvcrt
    win = win_files()
    access = 0x00060000 | (0xC0000000 if flags & os.O_RDWR else 0x40000000 if flags & os.O_WRONLY else 0x80000000)
    disposition = 1 if flags & os.O_CREAT and flags & os.O_EXCL else 4 if flags & os.O_CREAT else 3
    # Do not truncate until the handle, final path and security have been checked.
    with win.descriptor() as sd:
        with win.handle(path, access, disposition, sd=sd) as handle:
            win.protect_handle(handle)
            # Duplicate the handle: the context closes the original, the fd owns the duplicate.
            duplicate = win.w.HANDLE()
            fn = win.k.DuplicateHandle
            fn.argtypes = [win.w.HANDLE, win.w.HANDLE, win.w.HANDLE, ctypes.POINTER(win.w.HANDLE), win.w.DWORD, win.w.BOOL, win.w.DWORD]
            fn.restype = win.w.BOOL
            win.check(fn(win.current(), handle, win.current(), ctypes.byref(duplicate), 0, False, 2))
            try: fd = msvcrt.open_osfhandle(duplicate.value, flags & (os.O_APPEND | os.O_RDONLY | os.O_WRONLY | os.O_RDWR) | os.O_BINARY)
            except BaseException:
                win.close(duplicate); raise
    try:
        if flags & os.O_TRUNC: os.ftruncate(fd, 0)
        return fd
    except BaseException:
        os.close(fd); raise


def private_file(path):
    path = safe_path(path)
    if WINDOWS:
        win = win_files()
        with win.handle(path, 0x00060000) as handle: win.protect_handle(handle)
    else: os.chmod(path, 0o600)


def is_private(path):
    path = safe_path(path)
    if WINDOWS: return win_files().private(path, path.is_dir())
    return stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def acquire_worker_lock(directory):
    path = private_dir(directory) / "worker.lock"
    fd = open_private(path, os.O_CREAT | os.O_RDWR)
    try:
        if WINDOWS:
            import msvcrt
            if os.fstat(fd).st_size == 0: os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError(errno.EAGAIN, "Bridge worker already holds this lock") from exc
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd); raise


def configure_stdio():
    if WINDOWS:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")


def peak_memory_kib():
    if WINDOWS:
        win = win_files()
        class Counters(ctypes.Structure):
            _fields_ = [("cb", win.w.DWORD), ("PageFaultCount", win.w.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        info = Counters(); info.cb = ctypes.sizeof(info)
        fn = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        fn.argtypes = [win.w.HANDLE, ctypes.POINTER(Counters), win.w.DWORD]; fn.restype = win.w.BOOL
        win.check(fn(win.current(), ctypes.byref(info), info.cb))
        return info.PeakWorkingSetSize / 1024
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024 if sys.platform == "darwin" else value
