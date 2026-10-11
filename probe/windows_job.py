"""Native Windows process-tree limits for the foreground tunnel launcher.

Attach the launcher itself before creating any child. Windows then puts all
descendants in the same non-breakaway job. The handle intentionally stays open
until process exit; closing a kill-on-close job would terminate this process.
"""
import ctypes
from ctypes import wintypes as w
import sys


class BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', w.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', w.DWORD), ('Affinity', ctypes.c_size_t),
                ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]


class IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IOCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in
                ('TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                (name, w.DWORD) for name in
                ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]


class WindowsJob:
    def __init__(self, memory_mib=384, process_limit=64):
        if sys.platform != 'win32': raise RuntimeError('Windows Job Objects require Windows')
        if not 16 <= memory_mib <= 4096 or not 1 <= process_limit <= 64:
            raise ValueError('Invalid bounded resource limit')
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        def bind(name, restype, *args):
            fn = getattr(self.k, name); fn.restype = restype; fn.argtypes = args
            return fn
        current = bind('GetCurrentProcess', w.HANDLE)
        create = bind('CreateJobObjectW', w.HANDLE, ctypes.c_void_p, w.LPCWSTR)
        set_info = bind('SetInformationJobObject', w.BOOL, w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
        assign = bind('AssignProcessToJobObject', w.BOOL, w.HANDLE, w.HANDLE)
        self.query = bind('QueryInformationJobObject', w.BOOL, w.HANDLE, ctypes.c_int,
                          ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD))
        affinity = bind('GetProcessAffinityMask', w.BOOL, w.HANDLE,
                        ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t))
        process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
        self.check(affinity(current(), ctypes.byref(process_mask), ctypes.byref(system_mask)))
        mask = process_mask.value & -process_mask.value
        if not mask: raise OSError('No available processor affinity')
        self.handle = create(None, None)
        self.check(self.handle)
        info = ExtendedLimits()
        # ACTIVE_PROCESS | AFFINITY | JOB_MEMORY | KILL_ON_JOB_CLOSE.
        # No BREAKAWAY_OK or SILENT_BREAKAWAY_OK flags.
        info.BasicLimitInformation.LimitFlags = 0x8 | 0x10 | 0x200 | 0x2000
        info.BasicLimitInformation.ActiveProcessLimit = process_limit
        info.BasicLimitInformation.Affinity = mask
        info.JobMemoryLimit = memory_mib * 1024 * 1024
        try:
            self.check(set_info(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)))
            self.check(assign(self.handle, current()))
        except BaseException:
            close = bind('CloseHandle', w.BOOL, w.HANDLE)
            close(self.handle); self.handle = None
            raise

    @staticmethod
    def check(result):
        if not result: raise ctypes.WinError(ctypes.get_last_error())

    def metrics(self):
        limits, account = ExtendedLimits(), Accounting()
        self.check(self.query(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits), None))
        self.check(self.query(self.handle, 1, ctypes.byref(account), ctypes.sizeof(account), None))
        return {'memory_limit_bytes': limits.JobMemoryLimit,
                'peak_job_memory_used_bytes': limits.PeakJobMemoryUsed,
                'processor_affinity_mask': limits.BasicLimitInformation.Affinity,
                'process_limit': limits.BasicLimitInformation.ActiveProcessLimit,
                'active_processes': account.ActiveProcesses,
                'total_processes': account.TotalProcesses,
                'job_terminated_processes': account.TotalTerminatedProcesses,
                'oom_count': None,
                'oom_evidence': 'Windows Job accounting does not identify individual allocation failures'}
