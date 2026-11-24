"""Cross-platform non-blocking process lock used by local paper-bot jobs."""
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire_lock(path):
    fh = open(path, "a+b")
    try:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None

    fh.seek(1)
    fh.truncate()
    fh.write(f"{os.getpid()}\n".encode("ascii"))
    fh.flush()
    return fh


def release_lock(fh):
    if os.name == "nt":
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    fh.close()
