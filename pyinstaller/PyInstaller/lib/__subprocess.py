import sys
mswindows = (sys.platform == "win32")

import os
import types
import traceback
import gc
import signal
import json  # Use JSON for safe serialization

# Exception classes used by this module.
class CalledProcessError(Exception):
    """This exception is raised when a process run by check_call() returns
    a non-zero exit status.  The exit status will be stored in the
    returncode attribute."""
    def __init__(self, returncode, cmd):
        self.returncode = returncode
        self.cmd = cmd
    def __str__(self):
        return "Command '%s' returned non-zero exit status %d" % (self.cmd, self.returncode)


if mswindows:
    import threading
    import msvcrt
    if 1: # <-- change this to use pywin32 instead of the _subprocess driver
        import pywintypes
        from win32api import GetStdHandle, STD_INPUT_HANDLE, \
                             STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        from win32api import GetCurrentProcess, DuplicateHandle, \
                             GetModuleFileName, GetVersion
        from win32con import DUPLICATE_SAME_ACCESS, SW_HIDE
        from win32pipe import CreatePipe
        from win32process import CreateProcess, STARTUPINFO, \
                                 GetExitCodeProcess, STARTF_USESTDHANDLES, \
                                 STARTF_USESHOWWINDOW, CREATE_NEW_CONSOLE
        from win32process import TerminateProcess
        from win32event import WaitForSingleObject, INFINITE, WAIT_OBJECT_0
    else:
        from _subprocess import *
        class STARTUPINFO:
            dwFlags = 0
            hStdInput = None
            hStdOutput = None
            hStdError = None
            wShowWindow = 0
        class pywintypes:
            error = IOError
else:
    import select
    import errno
    import fcntl

__all__ = ["Popen", "PIPE", "STDOUT", "call", "check_call", "CalledProcessError"]

try:
    MAXFD = os.sysconf("SC_OPEN_MAX")
except:
    MAXFD = 256

_active = []

def _cleanup():
    for inst in _active[:]:
        if inst._internal_poll(_deadstate=sys.maxint) >= 0:
            try:
                _active.remove(inst)
            except ValueError:
                pass

PIPE = -1
STDOUT = -2


def _eintr_retry_call(func, *args):
    while True:
        try:
            return func(*args)
        except OSError as e:
            if e.errno == errno.EINTR:
                continue
            raise


def call(*popenargs, **kwargs):
    return Popen(*popenargs, **kwargs).wait()


def check_call(*popenargs, **kwargs):
    retcode = call(*popenargs, **kwargs)
    cmd = kwargs.get("args")
    if cmd is None:
        cmd = popenargs[0]
    if retcode:
        raise CalledProcessError(retcode, cmd)
    return retcode


def list2cmdline(seq):
    result = []
    needquote = False
    for arg in seq:
        bs_buf = []

        if result:
            result.append(' ')

        needquote = (" " in arg) or ("\t" in arg) or ("|" in arg) or not arg
        if needquote:
            result.append('"')

        for c in arg:
            if c == '\\':
                bs_buf.append(c)
            elif c == '"':
                result.append('\\' * len(bs_buf)*2)
                bs_buf = []
                result.append('\\"')
            else:
                if bs_buf:
                    result.extend(bs_buf)
                    bs_buf = []
                result.append(c)

        if bs_buf:
            result.extend(bs_buf)

        if needquote:
            result.extend(bs_buf)
            result.append('"')

    return ''.join(result)


class Popen(object):
    def __init__(self, args, bufsize=0, executable=None,
                 stdin=None, stdout=None, stderr=None,
                 preexec_fn=None, close_fds=False, shell=False,
                 cwd=None, env=None, universal_newlines=False,
                 startupinfo=None, creationflags=0):
        _cleanup()

        self._child_created = False
        if not isinstance(bufsize, (int, long)):
            raise TypeError("bufsize must be an integer")

        if mswindows:
            if preexec_fn is not None:
                raise ValueError("preexec_fn is not supported on Windows "
                                 "platforms")
            if close_fds and (stdin is not None or stdout is not None or
                              stderr is not None):
                raise ValueError("close_fds is not supported on Windows "
                                 "platforms if you redirect stdin/stdout/stderr")
        else:
            if startupinfo is not None:
                raise ValueError("startupinfo is only supported on Windows "
                                 "platforms")
            if creationflags != 0:
                raise ValueError("creationflags is only supported on Windows "
                                 "platforms")

        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.pid = None
        self.returncode = None
        self.universal_newlines = universal_newlines

        (p2cread, p2cwrite,
         c2pread, c2pwrite,
         errread, errwrite) = self._get_handles(stdin, stdout, stderr)

        self._execute_child(args, executable, preexec_fn, close_fds,
                            cwd, env, universal_newlines,
                            startupinfo, creationflags, shell,
                            p2cread, p2cwrite,
                            c2pread, c2pwrite,
                            errread, errwrite)

        if mswindows:
            if p2cwrite is not None:
                p2cwrite = msvcrt.open_osfhandle(p2cwrite.Detach(), 0)
            if c2pread is not None:
                c2pread = msvcrt.open_osfhandle(c2pread.Detach(), 0)
            if errread is not None:
                errread = msvcrt.open_osfhandle(errread.Detach(), 0)

        if p2cwrite is not None:
            self.stdin = os.fdopen(p2cwrite, 'wb', bufsize)
        if c2pread is not None:
            if universal_newlines:
                self.stdout = os.fdopen(c2pread, 'rU', bufsize)
            else:
                self.stdout = os.fdopen(c2pread, 'rb', bufsize)
        if errread is not None:
            if universal_newlines:
                self.stderr = os.fdopen(errread, 'rU', bufsize)
            else:
                self.stderr = os.fdopen(errread, 'rb', bufsize)


    def _translate_newlines(self, data):
        data = data.replace("\r\n", "\n")
        data = data.replace("\r", "\n")
        return data


    def __del__(self, sys=sys):
        if not self._child_created:
            return
        self._internal_poll(_deadstate=sys.maxint)
        if self.returncode is None and _active is not None:
            _active.append(self)


    def communicate(self, input=None):
        if [self.stdin, self.stdout, self.stderr].count(None) >= 2:
            stdout = None
            stderr = None
            if self.stdin:
                if input:
                    self.stdin.write(input)
                self.stdin.close()
            elif self.stdout:
                stdout = self.stdout.read()
                self.stdout.close()
            elif self.stderr:
                stderr = self.stderr.read()
                self.stderr.close()
            self.wait()
            return (stdout, stderr)

        return self._communicate(input)


    def poll(self):
        return self._internal_poll()


    if mswindows:
        def _get_handles(self, stdin, stdout, stderr):
            if stdin is None and stdout is None and stderr is None:
                return (None, None, None, None, None, None)

            p2cread, p2cwrite = None, None
            c2pread, c2pwrite = None, None
            errread, errwrite = None, None

            if stdin is None:
                p2cread = GetStdHandle(STD_INPUT_HANDLE)
                if p2cread is None:
                    p2cread, _ = CreatePipe(None, 0)
            elif stdin == PIPE:
                p2cread, p2cwrite = CreatePipe(None, 0)
            elif isinstance(stdin, int):
                p2cread = msvcrt.get_osfhandle(stdin)
            else:
                p2cread = msvcrt.get_osfhandle(stdin.fileno())
            p2cread = self._make_inheritable(p2cread)

            if stdout is None:
                c2pwrite = GetStdHandle(STD_OUTPUT_HANDLE)
                if c2pwrite is None:
                    _, c2pwrite = CreatePipe(None, 0)
            elif stdout == PIPE:
                c2pread, c2pwrite = CreatePipe(None, 0)
            elif isinstance(stdout, int):
                c2pwrite = msvcrt.get_osfhandle(stdout)
            else:
                c2pwrite = msvcrt.get_osfhandle(stdout.fileno())
            c2pwrite = self._make_inheritable(c2pwrite)

            if stderr is None:
                errwrite = GetStdHandle(STD_ERROR_HANDLE)
                if errwrite is None:
                    _, errwrite = CreatePipe(None, 0)
            elif stderr == PIPE:
                errread, errwrite = CreatePipe(None, 0)
            elif stderr == STDOUT:
                errwrite = c2pwrite
            elif isinstance(stderr, int):
                errwrite = msvcrt.get_osfhandle(stderr)
            else:
                errwrite = msvcrt.get_osfhandle(stderr.fileno())
            errwrite = self._make_inheritable(errwrite)

            return (p2cread, p2cwrite,
                    c2pread, c2pwrite,
                    errread, errwrite)


        def _make_inheritable(self, handle):
            return DuplicateHandle(GetCurrentProcess(), handle,
                                   GetCurrentProcess(), 0, 1,
                                   DUPLICATE_SAME_ACCESS)


        def _find_w9xpopen(self):
            w9xpopen = os.path.join(os.path.dirname(GetModuleFileName(0)),
                                    "w9xpopen.exe")
            if not os.path.exists(w9xpopen):
                w9xpopen = os.path.join(os.path.dirname(sys.exec_prefix),
                                        "w9xpopen.exe")
                if not os.path.exists(w9xpopen):
                    raise RuntimeError("Cannot locate w9xpopen.exe, which is "
                                       "needed for Popen to work with your "
                                       "shell or platform.")
            return w9xpopen


        def _execute_child(self, args, executable, preexec_fn, close_fds,
                           cwd, env, universal_newlines,
                           startupinfo, creationflags, shell,
                           p2cread, p2cwrite,
                           c2pread, c2pwrite,
                           errread, errwrite):
            if not isinstance(args, types.StringTypes):
                args = list2cmdline(args)

            if startupinfo is None:
                startupinfo = STARTUPINFO()
            if None not in (p2cread, c2pwrite, errwrite):
                startupinfo.dwFlags |= STARTF_USESTDHANDLES
                startupinfo.hStdInput = p2cread
                startupinfo.hStdOutput = c2pwrite
                startupinfo.hStdError = errwrite

            if shell:
                startupinfo.dwFlags |= STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = SW_HIDE
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                args = comspec + " /c " + args
                if (GetVersion() >= 0x80000000L or
                        os.path.basename(comspec).lower() == "command.com"):
                    w9xpopen = self._find_w9xpopen()
                    args = '"%s" %s' % (w9xpopen, args)
                    creationflags |= CREATE_NEW_CONSOLE

            try:
                hp, ht, pid, tid = CreateProcess(executable, args,
                                         None, None,
                                         int(not close_fds),
                                         creationflags,
                                         env,
                                         cwd,
                                         startupinfo)
            except pywintypes.error as e:
                raise WindowsError(*e.args)

            self._child_created = True
            self._handle = hp
            self.pid = pid
            ht.Close()

            if p2cread is not None:
                p2cread.Close()
            if c2pwrite is not None:
                c2pwrite.Close()
            if errwrite is not None:
                errwrite.Close()


        def _internal_poll(self, _deadstate=None):
            if self.returncode is None:
                if WaitForSingleObject(self._handle, 0) == WAIT_OBJECT_0:
                    self.returncode = GetExitCodeProcess(self._handle)
            return self.returncode


        def wait(self):
            if self.returncode is None:
                obj = WaitForSingleObject(self._handle, INFINITE)
                self.returncode = GetExitCodeProcess(self._handle)
            return self.returncode


        def _readerthread(self, fh, buffer):
            buffer.append(fh.read())


        def _communicate(self, input):
            stdout = None
            stderr = None

            if self.stdout:
                stdout = []
                stdout_thread = threading.Thread(target=self._readerthread,
                                                 args=(self.stdout, stdout))
                stdout_thread.setDaemon(True)
                stdout_thread.start()
            if self.stderr:
                stderr = []
                stderr_thread = threading.Thread(target=self._readerthread,
                                                 args=(self.stderr, stderr))
                stderr_thread.setDaemon(True)
                stderr_thread.start()

            if self.stdin:
                if input is not None:
                    self.stdin.write(input)
                self.stdin.close()

            if self.stdout:
                stdout_thread.join()
            if self.stderr:
                stderr_thread.join()

            if stdout is not None:
                stdout = stdout[0]
            if stderr is not None:
                stderr = stderr[0]

            if self.universal_newlines and hasattr(file, 'newlines'):
                if stdout:
                    stdout = self._translate_newlines(stdout)
                if stderr:
                    stderr = self._translate_newlines(stderr)

            self.wait()
            return (stdout, stderr)

        def send_signal(self, sig):
            if sig == signal.SIGTERM:
                self.terminate()
            else:
                raise ValueError("Only SIGTERM is supported on Windows")

        def terminate(self):
            TerminateProcess(self._handle, 1)

        kill = terminate

    else:
        def _get_handles(self, stdin, stdout, stderr):
            p2cread, p2cwrite = None, None
            c2pread, c2pwrite = None, None
            errread, errwrite = None, None

            if stdin is None:
                pass
            elif stdin == PIPE:
                p2cread, p2cwrite = os.pipe()
            elif isinstance(stdin, int):
                p2cread = stdin
            else:
                p2cread = stdin.fileno()

            if stdout is None:
                pass
            elif stdout == PIPE:
                c2pread, c2pwrite = os.pipe()
            elif isinstance(stdout, int):
                c2pwrite = stdout
            else:
                c2pwrite = stdout.fileno()

            if stderr is None:
                pass
            elif stderr == PIPE:
                errread, errwrite = os.pipe()
            elif stderr == STDOUT:
                errwrite = c2pwrite
            elif isinstance(stderr, int):
                errwrite = stderr
            else:
                errwrite = stderr.fileno()

            return (p2cread, p2cwrite,
                    c2pread, c2pwrite,
                    errread, errwrite)


        def _set_cloexec_flag(self, fd):
            try:
                cloexec_flag = fcntl.FD_CLOEXEC
            except AttributeError:
                cloexec_flag = 1

            old = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, old | cloexec_flag)


        def _close_fds(self, but):
            os.closerange(3, but)
            os.closerange(but + 1, MAXFD)


        def _execute_child(self, args, executable, preexec_fn, close_fds,
                           cwd, env, universal_newlines,
                           startupinfo, creationflags, shell,
                           p2cread, p2cwrite,
                           c2pread, c2pwrite,
                           errread, errwrite):
            if isinstance(args, types.StringTypes):
                args = [args]
            else:
                args = list(args)

            if shell:
                args = ["/bin/sh", "-c"] + args

            if executable is None:
                executable = args[0]

            errpipe_read, errpipe_write = os.pipe()
            try:
                try:
                    self._set_cloexec_flag(errpipe_write)

                    gc_was_enabled = gc.isenabled()
                    gc.disable()
                    try:
                        self.pid = os.fork()
                    except:
                        if gc_was_enabled:
                            gc.enable()
                        raise
                    self._child_created = True
                    if self.pid == 0:
                        try:
                            if p2cwrite is not None:
                                os.close(p2cwrite)
                            if c2pread is not None:
                                os.close(c2pread)
                            if errread is not None:
                                os.close(errread)
                            os.close(errpipe_read)

                            if p2cread is not None:
                                os.dup2(p2cread, 0)
                            if c2pwrite is not None:
                                os.dup2(c2pwrite, 1)
                            if errwrite is not None:
                                os.dup2(errwrite, 2)

                            if p2cread is not None and p2cread not in (0,):
                                os.close(p2cread)
                            if c2pwrite is not None and c2pwrite not in (p2cread, 1):
                                os.close(c2pwrite)
                            if errwrite is not None and errwrite not in (p2cread, c2pwrite, 2):
                                os.close(errwrite)

                            if close_fds:
                                self._close_fds(but=errpipe_write)

                            if cwd is not None:
                                os.chdir(cwd)

                            if preexec_fn:
                                preexec_fn()

                            if env is None:
                                os.execvp(executable, args)
                            else:
                                os.execvpe(executable, args, env)

                        except:
                            exc_type, exc_value, tb = sys.exc_info()
                            exc_lines = traceback.format_exception(exc_type,
                                                                   exc_value,
                                                                   tb)
                            exc_value.child_traceback = ''.join(exc_lines)
                            os.write(errpipe_write, json.dumps({
                                'exc_type': exc_type.__name__,
                                'exc_value': str(exc_value),
                                'child_traceback': exc_value.child_traceback
                            }).encode())

                        os._exit(255)

                    if gc_was_enabled:
                        gc.enable()
                finally:
                    os.close(errpipe_write)

                if p2cread is not None and p2cwrite is not None:
                    os.close(p2cread)
                if c2pwrite is not None and c2pread is not None:
                    os.close(c2pwrite)
                if errwrite is not None and errread is not None:
                    os.close(errwrite)

                data = _eintr_retry_call(os.read, errpipe_read, 1048576)
            finally:
                os.close(errpipe_read)

            if data != "":
                _eintr_retry_call(os.waitpid, self.pid, 0)
                child_exception_data = json.loads(data.decode())
                child_exception = RuntimeError(
                    child_exception_data['exc_value'])
                child_exception.child_traceback = child_exception_data['child_traceback']
                for fd in (p2cwrite, c2pread, errread):
                    if fd is not None:
                        os.close(fd)
                raise child_exception


        def _handle_exitstatus(self, sts):
            if os.WIFSIGNALED(sts):
                self.returncode = -os.WTERMSIG(sts)
            elif os.WIFEXITED(sts):
                self.returncode = os.WEXITSTATUS(sts)
            else:
                raise RuntimeError("Unknown child exit status!")


        def _internal_poll(self, _deadstate=None):
            if self.returncode is None:
                try:
                    pid, sts = os.waitpid(self.pid, os.WNOHANG)
                    if pid == self.pid:
                        self._handle_exitstatus(sts)
                except os.error:
                    if _deadstate is not None:
                        self.returncode = _deadstate
            return self.returncode


        def wait(self):
            if self.returncode is None:
                pid, sts = _eintr_retry_call(os.waitpid, self.pid, 0)
                self._handle_exitstatus(sts)
            return self.returncode


        def _communicate(self, input):
            read_set = []
            write_set = []
            stdout = None
            stderr = None

            if self.stdin:
                self.stdin.flush()
                if input:
                    write_set.append(self.stdin)
                else:
                    self.stdin.close()
            if self.stdout:
                read_set.append(self.stdout)
                stdout = []
            if self.stderr:
                read_set.append(self.stderr)
                stderr = []

            input_offset = 0
            while read_set or write_set:
                try:
                    rlist, wlist, xlist = select.select(read_set, write_set, [])
                except select.error as e:
                    if e.args[0] == errno.EINTR:
                        continue
                    raise

                if self.stdin in wlist:
                    chunk = input[input_offset : input_offset + 512]
                    bytes_written = os.write(self.stdin.fileno(), chunk)
                    input_offset += bytes_written
                    if input_offset >= len(input):
                        self.stdin.close()
                        write_set.remove(self.stdin)

                if self.stdout in rlist:
                    data = os.read(self.stdout.fileno(), 1024)
                    if data == "":
                        self.stdout.close()
                        read_set.remove(self.stdout)
                    stdout.append(data)

                if self.stderr in rlist:
                    data = os.read(self.stderr.fileno(), 1024)
                    if data == "":
                        self.stderr.close()
                        read_set.remove(self.stderr)
                    stderr.append(data)

            if stdout is not None:
                stdout = ''.join(stdout)
            if stderr is not None:
                stderr = ''.join(stderr)

            if self.universal_newlines and hasattr(file, 'newlines'):
                if stdout:
                    stdout = self._translate_newlines(stdout)
                if stderr:
                    stderr = self._translate_newlines(stderr)

            self.wait()
            return (stdout, stderr)

        def send_signal(self, sig):
            os.kill(self.pid, sig)

        def terminate(self):
            self.send_signal(signal.SIGTERM)

        def kill(self):
            self.send_signal(signal.SIGKILL)


def _demo_posix():
    plist = Popen(["ps"], stdout=PIPE).communicate()[0]
    print "Process list:"
    print plist

    if os.getuid() == 0:
        p = Popen(["id"], preexec_fn=lambda: os.setuid(100))
        p.wait()

    print "Looking for 'hda'..."
    p1 = Popen(["dmesg"], stdout=PIPE)
    p2 = Popen(["grep", "hda"], stdin=p1.stdout, stdout=PIPE)
    print repr(p2.communicate()[0])

    print
    print "Trying a weird file..."
    try:
        print Popen(["/this/path/does/not/exist"]).communicate()
    except OSError as e:
        if e.errno == errno.ENOENT:
            print "The file didn't exist.  I thought so..."
            print "Child traceback:"
            print e.child_traceback
        else:
            print "Error", e.errno
    else:
        print >>sys.stderr, "Gosh.  No error."


def _demo_windows():
    print "Looking for 'PROMPT' in set output..."
    p1 = Popen("set", stdout=PIPE, shell=True)
    p2 = Popen('find "PROMPT"', stdin=p1.stdout, stdout=PIPE)
    print repr(p2.communicate()[0])

    print "Executing calc..."
    p = Popen("calc")
    p.wait()


if __name__ == "__main__":
    if mswindows:
        _demo_windows()
    else:
        _demo_posix()
